"""Checks against the real Personal API.

Skipped by default. Run with a key configured (``gcpapi setup`` or
``GARMIN_PERSONAL_API_KEY``)::

    uv run pytest -m live

Besides calling every endpoint, these tests fail when Garmin returns fields the
models do not know, which is how changes to the API that the spec does not announce
are detected.

These tests run in public CI logs, so they must never print personal data: every
check goes through :func:`check`, whose failure message is fixed text (field names
at most), and plain ``assert`` is not used because pytest would print the values.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from gcpapi_sync import ActivityFile, AsyncGarminClient, BadRequestError, GarminClient, models
from gcpapi_sync.models import GarminModel

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = pytest.mark.live

TODAY = datetime.now(UTC).date()
YESTERDAY = TODAY - timedelta(days=1)
WEEK_AGO = TODAY - timedelta(days=7)


@pytest.fixture(scope="module")
def garmin() -> Iterator[GarminClient]:
    with GarminClient() as client:
        yield client


@pytest.fixture(scope="module")
def activity_id(garmin: GarminClient) -> int:
    activities = garmin.search_activities(limit=1)
    if not activities or activities[0].activity_id is None:
        pytest.skip("the account has no activities")
    return activities[0].activity_id


def check(condition: bool, message: str) -> None:
    """Fail with ``message`` only, without pytest's introspection of the values."""
    if not condition:
        pytest.fail(message, pytrace=False)


def undocumented(value: object, prefix: str = "") -> set[str]:
    """Paths of fields that ended up in ``model_extra`` (names only, no values)."""
    found: set[str] = set()
    if isinstance(value, GarminModel):
        name = prefix or type(value).__name__
        found |= {f"{name}.{key}" for key in value.model_extra or {}}
        for field in type(value).model_fields:
            found |= undocumented(getattr(value, field), f"{name}.{field}")
    elif isinstance(value, list):
        for item in value:
            found |= undocumented(item, prefix)
    return found


CALLS: dict[str, Callable[[GarminClient], object]] = {
    "get_user_profile_settings": lambda g: g.get_user_profile_settings(),
    "get_heart_rate_zones": lambda g: g.get_heart_rate_zones(),
    "get_fitness_age": lambda g: g.get_fitness_age(YESTERDAY),
    "get_daily_summary": lambda g: g.get_daily_summary(YESTERDAY),
    "get_daily_summaries": lambda g: g.get_daily_summaries(TODAY - timedelta(days=40), YESTERDAY),
    "get_daily_sleep": lambda g: g.get_daily_sleep(YESTERDAY),
    "get_daily_sleep_summaries": lambda g: g.get_daily_sleep_summaries(
        TODAY - timedelta(days=40), YESTERDAY
    ),
    "get_daily_heart_rate": lambda g: g.get_daily_heart_rate(YESTERDAY),
    "get_daily_hrv": lambda g: g.get_daily_hrv(YESTERDAY),
    "get_daily_hrv_summaries": lambda g: g.get_daily_hrv_summaries(
        TODAY - timedelta(days=400), YESTERDAY
    ),
    "get_daily_stress": lambda g: g.get_daily_stress(TODAY - timedelta(days=40), YESTERDAY),
    "get_daily_steps": lambda g: g.get_daily_steps(TODAY - timedelta(days=40), YESTERDAY),
    "get_daily_hydration": lambda g: g.get_daily_hydration(TODAY - timedelta(days=40), YESTERDAY),
    "get_daily_body_battery": lambda g: g.get_daily_body_battery(WEEK_AGO, YESTERDAY, chunks=4),
    "get_daily_spo2": lambda g: g.get_daily_spo2(YESTERDAY),
    "get_weight_range": lambda g: g.get_weight_range(
        TODAY - timedelta(days=30), YESTERDAY, include_all=True
    ),
    "get_effective_weight_goals": lambda g: g.get_effective_weight_goals(WEEK_AGO, YESTERDAY),
    "get_daily_food_logs": lambda g: g.get_daily_food_logs(YESTERDAY),
    "get_menstrual_cycle_summary": lambda g: g.get_menstrual_cycle_summary(YESTERDAY),
    "get_menstrual_cycle_day_view": lambda g: g.get_menstrual_cycle_day_view(
        YESTERDAY, today=TODAY
    ),
    "get_wellness_activity_summaries": lambda g: g.get_wellness_activity_summaries(
        TODAY - timedelta(days=365), TODAY
    ),
    "search_activities": lambda g: g.search_activities(
        limit=50, start_date=TODAY - timedelta(days=90)
    ),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_endpoint(garmin: GarminClient, name: str) -> None:
    result = CALLS[name](garmin)

    check(result is None or isinstance(result, GarminModel | list), f"{name}: unexpected type")
    unknown = undocumented(result)
    check(not unknown, f"{name} returned fields the models do not know: {sorted(unknown)}")


def test_activity_endpoints(garmin: GarminClient, activity_id: int) -> None:
    activity = garmin.get_activity(activity_id)
    workouts = garmin.get_activity_workouts(activity_id)
    download = garmin.download_activity(activity_id)

    check(activity.activity_id == activity_id, "get_activity returned a different activity")
    check(isinstance(workouts, list), "get_activity_workouts did not return a list")
    check(isinstance(download, ActivityFile), "download_activity did not return a file")
    check(len(download.content) > 0, "download_activity returned an empty file")
    unknown = undocumented(activity) | undocumented(workouts)
    check(not unknown, f"activity endpoints returned unknown fields: {sorted(unknown)}")


def test_iterating_activities_matches_search(garmin: GarminClient) -> None:
    since = TODAY - timedelta(days=60)

    iterated = [a.activity_id for a in garmin.iter_activities(start_date=since, page_size=15)]
    searched = [
        a.activity_id for a in garmin.search_activities(start_date=since, limit=len(iterated) + 5)
    ]

    check(iterated == searched, "iter_activities and search_activities disagree")


def test_ranges_are_contiguous(garmin: GarminClient) -> None:
    start = TODAY - timedelta(days=100)

    steps = garmin.get_daily_steps(start, YESTERDAY)

    days = [item.calendar_date for item in steps if item.calendar_date is not None]
    check(len(days) == len(steps), "some daily steps have no calendar date")
    check(days == sorted(days), "windows are not returned in order")
    check(len(days) == len(set(days)), "windows overlap")
    check(days[0] == start, "the first window does not start at the requested date")


def test_typed_series(garmin: GarminClient) -> None:
    heart_rate = garmin.get_daily_heart_rate(YESTERDAY)

    samples = heart_rate.heart_rate_samples()

    check(all(sample.timestamp.tzinfo is UTC for sample in samples), "timestamps are not UTC")
    in_order = samples == sorted(samples, key=lambda sample: sample.timestamp)
    check(in_order, "heart rate samples are not in chronological order")


def test_server_still_rejects_oversized_requests(garmin: GarminClient) -> None:
    """If this starts failing, Garmin relaxed a limit and _constants.py can be updated."""
    with pytest.raises(BadRequestError):
        garmin.get_json(
            f"/usersummary-service/stats/steps/daily/{TODAY - timedelta(days=60)}/{TODAY}"
        )


def test_weight_entries_endpoint_status(garmin: GarminClient) -> None:
    """Documented but broken server-side (HTTP 400) as of October 2026."""
    try:
        entries = garmin.get_weight_entries(WEEK_AGO, YESTERDAY)
    except BadRequestError:
        pytest.xfail("Garmin still answers /weight-service/weight/all with HTTP 400")
    unknown = undocumented(entries)
    check(not unknown, f"weight entries returned unknown fields: {sorted(unknown)}")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_async_client(anyio_backend: str) -> None:
    check(anyio_backend == "asyncio", "unexpected backend")
    async with AsyncGarminClient() as garmin:
        steps = await garmin.get_daily_steps(TODAY - timedelta(days=90), YESTERDAY)
        sleep = await garmin.get_daily_sleep(YESTERDAY)

    check(len(steps) >= 80, "the async client returned too few days")
    check(isinstance(sleep, models.DailySleep), "get_daily_sleep returned an unexpected type")
