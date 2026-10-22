"""Synchronous client."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Self, TypeVar, Unpack

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
from .exceptions import APIConnectionError, APITimeoutError

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Iterator, Sequence
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

__all__ = ["GarminClient"]

T = TypeVar("T")


class GarminClient(BaseClient):
    """Synchronous client for the Garmin Connect Personal API.

    The client exchanges the API key for an access token on first use, caches it,
    and renews it shortly before it expires or when the server rejects it. Requests
    that fail transiently are retried (see :class:`RetryConfig`). Endpoints with a
    maximum date range accept any range and split it into several requests.

    Use it as a context manager, or call :meth:`close` when done::

        with GarminClient() as garmin:
            sleep = garmin.get_daily_sleep("2026-10-21")

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
        http_client: Bring your own :class:`httpx.Client` (proxies, custom
            transports, ...). The caller remains responsible for closing it.

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
        http_client: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            access_token=access_token,
            key_file=key_file,
            token_cache=token_cache,
            base_url=base_url,
            retry=retry,
            user_agent=user_agent,
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout, follow_redirects=False)
        self._token_lock = threading.Lock()

    def close(self) -> None:
        """Close the underlying HTTP connection pool (unless it was passed in)."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # -- authentication ----------------------------------------------------------------

    def get_access_token(self, *, force_refresh: bool = False) -> AccessToken:
        """Return a valid access token, exchanging the API key if needed.

        Args:
            force_refresh: Always exchange the API key for a new token.
        """
        with self._token_lock:
            if not force_refresh and (token := self._usable_token()) is not None:
                return token
            return self._exchange_key()

    def _token(self, stale: str | None = None) -> str:
        with self._token_lock:
            token = self._usable_token(stale)
            return (token or self._exchange_key()).value

    def _exchange_key(self) -> AccessToken:
        url, headers = self._token_request()
        token = self._parse_token_response(self._send("POST", url, headers, ()))
        self._store_token(token)
        return token

    # -- transport ---------------------------------------------------------------------

    def _send(
        self, method: str, url: str, headers: dict[str, str], params: ep.Params
    ) -> httpx.Response:
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                response = self._http.request(method, url, headers=headers, params=params)
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
                response.close()
            time.sleep(delay)
            attempt += 1

    def _request(self, request: Request[T]) -> T:
        url = self._url(request.path)
        token = self._token()
        headers = self._headers(token, request.accept)
        response = self._send("GET", url, headers, request.params)
        if response.status_code == 401 and self.can_refresh:
            # The token was revoked or expired early: renew it once and retry.
            headers = self._headers(self._token(stale=token), request.accept)
            response = self._send("GET", url, headers, request.params)
        if response.is_error:
            raise self._status_error(response)
        return request.parse(response)

    def _collect(
        self,
        build: Callable[[date, date], Request[list[T]]],
        start: DateLike,
        end: DateLike,
        max_days: int,
    ) -> list[T]:
        first, last = to_range(start, end)
        items: list[T] = []
        for window_start, window_end in windows(first, last, max_days):
            chunk: list[T] = self._request(build(window_start, window_end))
            items.extend(chunk)
        return items

    def get_json(self, path: str, params: Sequence[tuple[str, str]] = ()) -> JsonValue:
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
        return self._request(ep.get_json(path, params))

    # -- activities --------------------------------------------------------------------

    def get_activity(self, activity_id: int) -> ActivitySummary:
        """Return an activity's summary, metadata, measurements and splits."""
        return self._request(ep.get_activity(activity_id))

    def get_activity_workouts(self, activity_id: int) -> list[ActivityWorkout]:
        """Return the structured workouts performed during an activity."""
        return self._request(ep.get_activity_workouts(activity_id))

    def search_activities(
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
            page = self._request(ep.search_activities(start + len(activities), size, params))
            activities.extend(page)
            if len(page) < size:
                break
        return activities

    def iter_activities(
        self,
        *,
        start: int = 0,
        page_size: int = DEFAULT_ACTIVITY_PAGE_SIZE,
        **filters: Unpack[ActivityFilters],
    ) -> Iterator[SummarizedActivity]:
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
            page = self._request(ep.search_activities(offset, page_size, params))
            yield from page
            if len(page) < page_size:
                return
            offset += len(page)

    def download_activity(self, activity_id: int) -> ActivityFile:
        """Download an activity in its original format (usually FIT)."""
        return self._request(ep.download_activity(activity_id))

    def download_activity_zip(self, activity_id: int) -> bytes:
        """Download an activity as the zip archive Garmin serves."""
        return self._request(ep.download_activity_zip(activity_id))

    # -- daily summaries ---------------------------------------------------------------

    def get_daily_summary(self, day: DateLike) -> UserDailySummary:
        """Return the day's wellness summary: steps, calories, stress, Body Battery, ..."""
        return self._request(ep.get_daily_summary(day))

    def get_daily_summaries(self, start: DateLike, end: DateLike) -> list[UserDailySummaryV2]:
        """Return detailed daily summaries for each day from ``start`` to ``end`` (inclusive)."""
        return self._collect(ep.get_daily_summaries, start, end, MAX_DAYS_DAILY_SUMMARIES)

    def get_wellness_activity_summaries(
        self, start: DateLike, end: DateLike
    ) -> list[WellnessActivitySummary]:
        """Return wellness activities (such as breathing sessions) in a date range."""
        first, last = to_range(start, end)
        return self._request(ep.get_wellness_activity_summaries(first, last))

    # -- sleep -------------------------------------------------------------------------

    def get_daily_sleep(self, day: DateLike) -> DailySleep:
        """Return the sleep record for a calendar day (the night ending that morning)."""
        return self._request(ep.get_daily_sleep(day))

    def get_daily_sleep_summaries(self, start: DateLike, end: DateLike) -> list[DailySleep]:
        """Return sleep records for each day from ``start`` to ``end`` (inclusive)."""
        return self._collect(ep.get_daily_sleep_summaries, start, end, MAX_DAYS_SLEEP)

    # -- heart -------------------------------------------------------------------------

    def get_daily_heart_rate(self, day: DateLike) -> DailyHeartRate:
        """Return the day's heart rate time series and resting/min/max values."""
        return self._request(ep.get_daily_heart_rate(day))

    def get_heart_rate_zones(self, *, generate_defaults: bool = True) -> list[HeartRateZones]:
        """Return the user's heart rate zone configuration per sport.

        Args:
            generate_defaults: Return default zones when none are configured.
        """
        return self._request(ep.get_heart_rate_zones(generate_defaults=generate_defaults))

    def get_daily_hrv(self, day: DateLike) -> HrvChart | None:
        """Return the night's HRV summary and readings, or ``None`` without data."""
        return self._request(ep.get_daily_hrv(day))

    def get_daily_hrv_summaries(self, start: DateLike, end: DateLike) -> list[HrvSummary]:
        """Return daily HRV summaries from ``start`` to ``end`` (inclusive)."""
        return self._collect(ep.get_daily_hrv_summaries, start, end, MAX_DAYS_HRV)

    # -- wellness ----------------------------------------------------------------------

    def get_daily_stress(self, start: DateLike, end: DateLike) -> list[MultiValueWellnessChartItem]:
        """Return daily stress levels and durations from ``start`` to ``end`` (inclusive)."""
        return self._collect(ep.get_daily_stress, start, end, MAX_DAYS_STRESS)

    def get_daily_steps(self, start: DateLike, end: DateLike) -> list[StepsDailyStat]:
        """Return daily steps, distance and goal from ``start`` to ``end`` (inclusive)."""
        return self._collect(ep.get_daily_steps, start, end, MAX_DAYS_STEPS)

    def get_daily_hydration(self, start: DateLike, end: DateLike) -> list[UserDailyHydration]:
        """Return daily hydration intake and goals from ``start`` to ``end`` (inclusive)."""
        return self._collect(ep.get_daily_hydration, start, end, MAX_DAYS_HYDRATION)

    def get_daily_body_battery(
        self, start: DateLike, end: DateLike, *, chunks: int | None = None
    ) -> list[BodyBatteryDailyReport]:
        """Return daily Body Battery reports from ``start`` to ``end`` (inclusive).

        Args:
            start: First day.
            end: Last day.
            chunks: Split each day into this many intervals and report the lowest and
                highest reading of each.
        """
        return self._collect(
            lambda a, b: ep.get_daily_body_battery(a, b, chunks=chunks),
            start,
            end,
            MAX_DAYS_BODY_BATTERY,
        )

    def get_daily_spo2(self, day: DateLike) -> DailySpO2AndAcclimation:
        """Return the day's pulse-ox (SpO2) readings and altitude acclimation."""
        return self._request(ep.get_daily_spo2(day))

    def get_fitness_age(self, day: DateLike, *, full_history_scan: bool = True) -> FitnessAge:
        """Return the latest Fitness Age as of ``day``.

        Args:
            day: Reference date.
            full_history_scan: Search the full history; ``False`` only looks at the
                28 days before ``day``.
        """
        return self._request(ep.get_fitness_age(day, full_history_scan=full_history_scan))

    # -- body composition --------------------------------------------------------------

    def get_weight_range(
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
        return self._request(ep.get_weight_range(first, last, include_all=include_all))

    def get_weight_entries(self, start: DateLike, end: DateLike) -> list[DateWeight]:
        """Return all weight measurements in a date range.

        Note:
            As of October 2026 Garmin answers this documented endpoint with HTTP 400
            for every input. Use ``get_weight_range(..., include_all=True)`` instead.
        """
        first, last = to_range(start, end)
        return self._request(ep.get_weight_entries(first, last))

    def get_effective_weight_goals(self, start: DateLike, end: DateLike) -> UserGoalsRange:
        """Return the weight goals in effect during a date range."""
        first, last = to_range(start, end)
        return self._request(ep.get_effective_weight_goals(first, last))

    # -- nutrition ---------------------------------------------------------------------

    def get_daily_food_logs(
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
        return self._request(
            ep.get_daily_food_logs(
                day,
                include_goals=include_goals,
                include_nutrition=include_nutrition,
                include_media=include_media,
                meal_id=meal_id,
            )
        )

    # -- women's health ----------------------------------------------------------------

    def get_menstrual_cycle_summary(self, day: DateLike) -> MenstrualCycleSummary | None:
        """Return the menstrual cycle summary for ``day``, or ``None`` if not tracked."""
        return self._request(ep.get_menstrual_cycle_summary(day))

    def get_menstrual_cycle_day_view(
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
        return self._request(
            ep.get_menstrual_cycle_day_view(
                day,
                today=today,
                reduced_response=reduced_response,
                skip_baby_movement=skip_baby_movement,
                include_analysis=include_analysis,
            )
        )

    # -- profile -----------------------------------------------------------------------

    def get_user_profile_settings(self) -> UserSettings:
        """Return the user's profile settings (units, measurements, preferences)."""
        return self._request(ep.get_user_profile_settings())
