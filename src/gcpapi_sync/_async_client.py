"""Asynchronous client."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Self, TypeVar, Unpack

import anyio
import httpx

from . import _endpoints as ep
from ._constants import (
    DEFAULT_ACTIVITY_PAGE_SIZE,
    DEFAULT_TIMEOUT,
    MAX_ACTIVITY_PAGE_SIZE,
    MAX_DAYS_BODY_BATTERY,
    MAX_DAYS_DAILY_SUMMARIES,
    MAX_DAYS_HRV,
    MAX_DAYS_HYDRATION,
    MAX_DAYS_SLEEP,
    MAX_DAYS_STEPS,
    MAX_DAYS_STRESS,
)
from ._core import BaseClient
from ._dates import DateLike, to_range, windows
from .exceptions import APIConnectionError, APITimeoutError, GarminError

if TYPE_CHECKING:
    import os
    from collections.abc import AsyncIterator, Callable, Sequence
    from datetime import date
    from types import TracebackType

    from pydantic import JsonValue

    from ._auth import AccessToken, TokenCache
    from ._endpoints import ActivityFilters, Request
    from ._files import ActivityFile
    from ._retry import RetryConfig
    from .models import (
        ActivitySummary,
        ActivityWorkout,
        BodyBatteryDailyReport,
        DailyHeartRate,
        DailyNutritionSummary,
        DailySleep,
        DailySpO2AndAcclimation,
        DateWeight,
        DetailedMenstrualCycleDay,
        FitnessAge,
        HeartRateZones,
        HrvChart,
        HrvSummary,
        MenstrualCycleSummary,
        MultiValueWellnessChartItem,
        StepsDailyStat,
        SummarizedActivity,
        UserDailyHydration,
        UserDailySummary,
        UserDailySummaryV2,
        UserGoalsRange,
        UserSettings,
        WeightRange,
        WellnessActivitySummary,
    )

__all__ = ["AsyncGarminClient"]

T = TypeVar("T")


class AsyncGarminClient(BaseClient):
    """Asynchronous client for the Garmin Connect Personal API.

    Same methods as :class:`GarminClient`, as coroutines. Works with asyncio and trio.
    Date ranges that need several requests are fetched concurrently::

        async with AsyncGarminClient() as garmin:
            steps = await garmin.get_daily_steps("2026-01-01", "2026-10-21")

    Args:
        api_key: Personal API key. If omitted, it is read from ``key_file``, the
            ``GARMIN_PERSONAL_API_KEY`` environment variable, or
            ``~/.config/garmin/connect-personal-api-key``, in that order.
        access_token: An already issued access token. Without an API key the client
            uses it until it expires.
        key_file: File containing the API key.
        token_cache: Where access tokens are kept. Defaults to memory; use
            :class:`FileTokenCache` to reuse tokens across processes.
        base_url: API host. Defaults to ``GARMIN_PERSONAL_API_BASE_URL`` or the
            production host.
        timeout: Request timeout in seconds, or an :class:`httpx.Timeout`.
        retry: Retry policy. ``RetryConfig(max_retries=0)`` disables retries.
        user_agent: Override the ``User-Agent`` header.
        http_client: Bring your own :class:`httpx.AsyncClient`. The caller remains
            responsible for closing it.
        max_concurrency: Maximum parallel requests when a date range is split.

    Raises:
        ConfigurationError: No API key or access token could be found.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        access_token: str | None = None,
        key_file: str | os.PathLike[str] | None = None,
        token_cache: TokenCache | None = None,
        base_url: str | None = None,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT,
        retry: RetryConfig | None = None,
        user_agent: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        max_concurrency: int = 4,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        super().__init__(
            api_key=api_key,
            access_token=access_token,
            key_file=key_file,
            token_cache=token_cache,
            base_url=base_url,
            retry=retry,
            user_agent=user_agent,
        )
        self.max_concurrency = max_concurrency
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        self._token_lock = anyio.Lock()

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool (unless it was passed in)."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- authentication ----------------------------------------------------------------

    async def get_access_token(self, *, force_refresh: bool = False) -> AccessToken:
        """Return a valid access token, exchanging the API key if needed.

        Args:
            force_refresh: Always exchange the API key for a new token.
        """
        async with self._token_lock:
            if not force_refresh and (token := self._usable_token()) is not None:
                return token
            return await self._exchange_key()

    async def _token(self, stale: str | None = None) -> str:
        async with self._token_lock:
            token = self._usable_token(stale)
            return (token or await self._exchange_key()).value

    async def _exchange_key(self) -> AccessToken:
        url, headers = self._token_request()
        token = self._parse_token_response(await self._send("POST", url, headers, ()))
        self._store_token(token)
        return token

    # -- transport ---------------------------------------------------------------------

    async def _send(
        self, method: str, url: str, headers: dict[str, str], params: ep.Params
    ) -> httpx.Response:
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                response = await self._http.request(method, url, headers=headers, params=params)
            except httpx.TransportError as exc:
                delay = self._retry_delay(attempt, None)
                if delay is None:
                    error = (
                        APITimeoutError
                        if isinstance(exc, httpx.TimeoutException)
                        else APIConnectionError
                    )
                    raise error(
                        f"{method} {httpx.URL(url).path} failed: {exc}", request=exc.request
                    ) from exc
                self._log_retry(attempt, delay, repr(exc))
            else:
                self._log_response(response, time.monotonic() - started)
                delay = self._retry_delay(attempt, response)
                if delay is None:
                    return response
                self._log_retry(attempt, delay, f"HTTP {response.status_code}")
                await response.aclose()
            await anyio.sleep(delay)
            attempt += 1

    async def _request(self, request: Request[T]) -> T:
        url = self._url(request.path)
        token = await self._token()
        headers = self._headers(token, request.accept)
        response = await self._send("GET", url, headers, request.params)
        if response.status_code == 401 and self.can_refresh:
            # The token was revoked or expired early: renew it once and retry.
            headers = self._headers(await self._token(stale=token), request.accept)
            response = await self._send("GET", url, headers, request.params)
        if response.is_error:
            raise self._status_error(response)
        return request.parse(response)

    async def _collect(
        self,
        build: Callable[[date, date], Request[list[T]]],
        start: DateLike,
        end: DateLike,
        max_days: int,
    ) -> list[T]:
        first, last = to_range(start, end)
        spans = windows(first, last, max_days)
        if len(spans) == 1:
            return await self._request(build(*spans[0]))
        results: list[list[T]] = [[] for _ in spans]
        errors: list[Exception] = []
        limiter = anyio.CapacityLimiter(self.max_concurrency)

        async def fetch(index: int, window_start: date, window_end: date) -> None:
            try:
                async with limiter:
                    results[index] = await self._request(build(window_start, window_end))
            except GarminError as exc:
                errors.append(exc)
                group.cancel_scope.cancel()

        async with anyio.create_task_group() as group:
            for index, (window_start, window_end) in enumerate(spans):
                group.start_soon(fetch, index, window_start, window_end)
        if errors:
            # Raise the API error itself rather than an ExceptionGroup.
            raise errors[0]
        return [item for chunk in results for item in chunk]

    async def get_json(self, path: str, params: Sequence[tuple[str, str]] = ()) -> JsonValue:
        """GET any path on the API host and return the decoded JSON.

        An escape hatch for endpoints this package does not wrap yet. Authentication,
        retries and error handling work as for the typed methods.

        Args:
            path: Absolute path, such as ``"/sleep-service/sleep/dailySleep"``.
            params: Query parameters as ``(name, value)`` pairs; repeat a name to
                send it several times.

        Returns:
            The decoded JSON, or ``None`` for an empty response.
        """
        return await self._request(ep.get_json(path, params))

    # -- activities --------------------------------------------------------------------

    async def get_activity(self, activity_id: int) -> ActivitySummary:
        """Return an activity's summary, metadata, measurements and splits."""
        return await self._request(ep.get_activity(activity_id))

    async def get_activity_workouts(self, activity_id: int) -> list[ActivityWorkout]:
        """Return the structured workouts performed during an activity."""
        return await self._request(ep.get_activity_workouts(activity_id))

    async def search_activities(
        self,
        *,
        start: int = 0,
        limit: int = DEFAULT_ACTIVITY_PAGE_SIZE,
        **filters: Unpack[ActivityFilters],
    ) -> list[SummarizedActivity]:
        """Return up to ``limit`` activities matching ``filters``, newest first by default.

        Large limits are fetched in several pages (the server fails on big pages).

        Args:
            start: Zero-based offset of the first activity.
            limit: Maximum number of activities to return.
            **filters: See :class:`ActivityFilters`.
        """
        if start < 0 or limit < 0:
            raise ValueError("start and limit must be >= 0")
        params = ep.activity_filter_params(filters)
        activities: list[SummarizedActivity] = []
        while len(activities) < limit:
            size = min(limit - len(activities), DEFAULT_ACTIVITY_PAGE_SIZE)
            page = await self._request(ep.search_activities(start + len(activities), size, params))
            activities.extend(page)
            if len(page) < size:
                break
        return activities

    async def iter_activities(
        self,
        *,
        start: int = 0,
        page_size: int = DEFAULT_ACTIVITY_PAGE_SIZE,
        **filters: Unpack[ActivityFilters],
    ) -> AsyncIterator[SummarizedActivity]:
        """Iterate over all activities matching ``filters``, fetching pages lazily.

        Args:
            start: Zero-based offset of the first activity.
            page_size: Activities per request (1 to 45).
            **filters: See :class:`ActivityFilters`.
        """
        if not 1 <= page_size <= MAX_ACTIVITY_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_ACTIVITY_PAGE_SIZE}")
        if start < 0:
            raise ValueError("start must be >= 0")
        params = ep.activity_filter_params(filters)
        offset = start
        while True:
            page = await self._request(ep.search_activities(offset, page_size, params))
            for activity in page:
                yield activity
            if len(page) < page_size:
                return
            offset += len(page)

    async def download_activity(self, activity_id: int) -> ActivityFile:
        """Download an activity in its original format (usually FIT)."""
        return await self._request(ep.download_activity(activity_id))

    async def download_activity_zip(self, activity_id: int) -> bytes:
        """Download an activity as the zip archive Garmin serves."""
        return await self._request(ep.download_activity_zip(activity_id))

    # -- daily summaries ---------------------------------------------------------------

    async def get_daily_summary(self, day: DateLike) -> UserDailySummary:
        """Return the day's wellness summary: steps, calories, stress, Body Battery, ..."""
        return await self._request(ep.get_daily_summary(day))

    async def get_daily_summaries(self, start: DateLike, end: DateLike) -> list[UserDailySummaryV2]:
        """Return detailed daily summaries for each day from ``start`` to ``end`` (inclusive)."""
        return await self._collect(ep.get_daily_summaries, start, end, MAX_DAYS_DAILY_SUMMARIES)

    async def get_wellness_activity_summaries(
        self, start: DateLike, end: DateLike
    ) -> list[WellnessActivitySummary]:
        """Return wellness activities (such as breathing sessions) in a date range."""
        first, last = to_range(start, end)
        return await self._request(ep.get_wellness_activity_summaries(first, last))

    # -- sleep -------------------------------------------------------------------------

    async def get_daily_sleep(self, day: DateLike) -> DailySleep:
        """Return the sleep record for a calendar day (the night ending that morning)."""
        return await self._request(ep.get_daily_sleep(day))

    async def get_daily_sleep_summaries(self, start: DateLike, end: DateLike) -> list[DailySleep]:
        """Return sleep records for each day from ``start`` to ``end`` (inclusive)."""
        return await self._collect(ep.get_daily_sleep_summaries, start, end, MAX_DAYS_SLEEP)

    # -- heart -------------------------------------------------------------------------

    async def get_daily_heart_rate(self, day: DateLike) -> DailyHeartRate:
        """Return the day's heart rate time series and resting/min/max values."""
        return await self._request(ep.get_daily_heart_rate(day))

    async def get_heart_rate_zones(self, *, generate_defaults: bool = True) -> list[HeartRateZones]:
        """Return the user's heart rate zone configuration per sport.

        Args:
            generate_defaults: Return default zones when none are configured.
        """
        return await self._request(ep.get_heart_rate_zones(generate_defaults=generate_defaults))

    async def get_daily_hrv(self, day: DateLike) -> HrvChart | None:
        """Return the night's HRV summary and readings, or ``None`` without data."""
        return await self._request(ep.get_daily_hrv(day))

    async def get_daily_hrv_summaries(self, start: DateLike, end: DateLike) -> list[HrvSummary]:
        """Return daily HRV summaries from ``start`` to ``end`` (inclusive)."""
        return await self._collect(ep.get_daily_hrv_summaries, start, end, MAX_DAYS_HRV)

    # -- wellness ----------------------------------------------------------------------

    async def get_daily_stress(
        self, start: DateLike, end: DateLike
    ) -> list[MultiValueWellnessChartItem]:
        """Return daily stress levels and durations from ``start`` to ``end`` (inclusive)."""
        return await self._collect(ep.get_daily_stress, start, end, MAX_DAYS_STRESS)

    async def get_daily_steps(self, start: DateLike, end: DateLike) -> list[StepsDailyStat]:
        """Return daily steps, distance and goal from ``start`` to ``end`` (inclusive)."""
        return await self._collect(ep.get_daily_steps, start, end, MAX_DAYS_STEPS)

    async def get_daily_hydration(self, start: DateLike, end: DateLike) -> list[UserDailyHydration]:
        """Return daily hydration intake and goals from ``start`` to ``end`` (inclusive)."""
        return await self._collect(ep.get_daily_hydration, start, end, MAX_DAYS_HYDRATION)

    async def get_daily_body_battery(
        self, start: DateLike, end: DateLike, *, chunks: int | None = None
    ) -> list[BodyBatteryDailyReport]:
        """Return daily Body Battery reports from ``start`` to ``end`` (inclusive).

        Args:
            start: First day.
            end: Last day.
            chunks: Split each day into this many intervals and report the lowest and
                highest reading of each.
        """
        return await self._collect(
            lambda a, b: ep.get_daily_body_battery(a, b, chunks=chunks),
            start,
            end,
            MAX_DAYS_BODY_BATTERY,
        )

    async def get_daily_spo2(self, day: DateLike) -> DailySpO2AndAcclimation:
        """Return the day's pulse-ox (SpO2) readings and altitude acclimation."""
        return await self._request(ep.get_daily_spo2(day))

    async def get_fitness_age(self, day: DateLike, *, full_history_scan: bool = True) -> FitnessAge:
        """Return the latest Fitness Age as of ``day``.

        Args:
            day: Reference date.
            full_history_scan: Search the full history; ``False`` only looks at the
                28 days before ``day``.
        """
        return await self._request(ep.get_fitness_age(day, full_history_scan=full_history_scan))

    # -- body composition --------------------------------------------------------------

    async def get_weight_range(
        self, start: DateLike, end: DateLike, *, include_all: bool = False
    ) -> WeightRange:
        """Return weight summaries for a date range: per-day values and the range average.

        Weights are in grams. Garmin computes the average over the whole range, so the
        range is sent as one request; ranges beyond about a year may fail server-side.

        Args:
            start: First day.
            end: Last day.
            include_all: Include every measurement of each day, not just the latest.
        """
        first, last = to_range(start, end)
        return await self._request(ep.get_weight_range(first, last, include_all=include_all))

    async def get_weight_entries(self, start: DateLike, end: DateLike) -> list[DateWeight]:
        """Return all weight measurements in a date range.

        Note:
            As of October 2026 Garmin answers this documented endpoint with HTTP 400
            for every input. Use ``get_weight_range(..., include_all=True)`` instead.
        """
        first, last = to_range(start, end)
        return await self._request(ep.get_weight_entries(first, last))

    async def get_effective_weight_goals(self, start: DateLike, end: DateLike) -> UserGoalsRange:
        """Return the weight goals in effect during a date range."""
        first, last = to_range(start, end)
        return await self._request(ep.get_effective_weight_goals(first, last))

    # -- nutrition ---------------------------------------------------------------------

    async def get_daily_food_logs(
        self,
        day: DateLike,
        *,
        include_goals: bool = True,
        include_nutrition: bool = True,
        include_media: bool = True,
        meal_id: int | None = None,
    ) -> DailyNutritionSummary:
        """Return the day's nutrition log, goals and per-meal breakdown.

        Args:
            day: Calendar day.
            include_goals: Include nutrition goals.
            include_nutrition: Include nutrient totals.
            include_media: Include food images.
            meal_id: Only this meal.
        """
        return await self._request(
            ep.get_daily_food_logs(
                day,
                include_goals=include_goals,
                include_nutrition=include_nutrition,
                include_media=include_media,
                meal_id=meal_id,
            )
        )

    # -- women's health ----------------------------------------------------------------

    async def get_menstrual_cycle_summary(self, day: DateLike) -> MenstrualCycleSummary | None:
        """Return the menstrual cycle summary for ``day``, or ``None`` if not tracked."""
        return await self._request(ep.get_menstrual_cycle_summary(day))

    async def get_menstrual_cycle_day_view(
        self,
        day: DateLike,
        *,
        today: DateLike | None = None,
        reduced_response: bool = False,
        skip_baby_movement: bool = False,
        include_analysis: bool = False,
    ) -> DetailedMenstrualCycleDay | None:
        """Return the detailed menstrual cycle view for ``day``, or ``None`` if not tracked.

        Args:
            day: Calendar day.
            today: Reference "today" for predictions.
            reduced_response: Request a lighter payload.
            skip_baby_movement: Omit kick-count data.
            include_analysis: Include cycle analysis.
        """
        return await self._request(
            ep.get_menstrual_cycle_day_view(
                day,
                today=today,
                reduced_response=reduced_response,
                skip_baby_movement=skip_baby_movement,
                include_analysis=include_analysis,
            )
        )

    # -- profile -----------------------------------------------------------------------

    async def get_user_profile_settings(self) -> UserSettings:
        """Return the user's profile settings (units, measurements, preferences)."""
        return await self._request(ep.get_user_profile_settings())
