"""Transport-independent descriptions of every Personal API endpoint.

Each function returns a :class:`Request`: the path, query parameters and a parser
for the response. The sync and async clients only differ in how they send it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Literal, NamedTuple, TypedDict, TypeVar

from pydantic import JsonValue, TypeAdapter, ValidationError

from ._dates import DateLike, to_date
from ._files import ActivityFile, extract_activity_file
from .exceptions import ResponseValidationError
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
    HrvDailySummary,
    HrvSummary,
    MenstrualCycleSummary,
    MultiValueWellnessChartItem,
    StepsDailyStat,
    SummarizedActivity,
    UserDailyHydration,
    UserDailySummary,
    UserDailySummaryV2,
    UserDailySummaryV2List,
    UserGoalsRange,
    UserSettings,
    WeightRange,
    WellnessActivitySummary,
)

if TYPE_CHECKING:
    from datetime import date

    import httpx

T = TypeVar("T")
U = TypeVar("U")
Params = tuple[tuple[str, str], ...]
Parser = Callable[["httpx.Response"], T]

JSON = "application/json"
ZIP = "application/x-zip-compressed"  # Garmin answers HTTP 406 for application/octet-stream


@dataclass(frozen=True, slots=True)
class Request(Generic[T]):
    """One GET request against the API and how to decode its response."""

    path: str
    parse: Parser[T]
    params: Params = ()
    accept: str = JSON


# --------------------------------------------------------------------------------------
# Response parsers
# --------------------------------------------------------------------------------------


def _is_empty(response: httpx.Response) -> bool:
    return response.status_code == 204 or not response.content.strip()


def _validate(adapter: TypeAdapter[T], response: httpx.Response) -> T:
    try:
        return adapter.validate_json(response.content)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "<root>"
        raise ResponseValidationError(
            f"GET {response.request.url.path} returned data that does not match the expected "
            f"model ({exc.error_count()} error(s); first at {location}: {first['msg']}). The raw "
            "payload is available as .raw; please report this at "
            "https://github.com/thomascamminady/gcpapi-sync/issues",
            response=response,
            validation_error=exc,
        ) from exc


def _parser(adapter: TypeAdapter[T], empty: Callable[[], T]) -> Parser[T]:
    def parse(response: httpx.Response) -> T:
        return empty() if _is_empty(response) else _validate(adapter, response)

    return parse


def _required(adapter: TypeAdapter[T]) -> Parser[T]:
    def parse(response: httpx.Response) -> T:
        if _is_empty(response):
            raise ResponseValidationError(
                f"GET {response.request.url.path} returned an empty response", response=response
            )
        return _validate(adapter, response)

    return parse


def _optional(adapter: TypeAdapter[T]) -> Parser[T | None]:
    def parse(response: httpx.Response) -> T | None:
        return None if _is_empty(response) else _validate(adapter, response)

    return parse


def _then(parser: Parser[T], transform: Callable[[T], U]) -> Parser[U]:
    def parse(response: httpx.Response) -> U:
        return transform(parser(response))

    return parse


def _json(response: httpx.Response) -> JsonValue:
    return None if _is_empty(response) else _validate(_JSON_VALUE, response)


_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_ACTIVITY = _required(TypeAdapter(ActivitySummary))
_ACTIVITY_WORKOUTS = _parser(TypeAdapter(list[ActivityWorkout]), list)
_ACTIVITIES = _parser(TypeAdapter(list[SummarizedActivity]), list)
_HEART_RATE_ZONES = _parser(TypeAdapter(list[HeartRateZones]), list)
_FITNESS_AGE = _parser(TypeAdapter(FitnessAge), FitnessAge)
_WEIGHT_GOALS = _parser(TypeAdapter(UserGoalsRange), UserGoalsRange)
_HRV = _optional(TypeAdapter(HrvChart))
_HRV_SUMMARIES = _then(
    _optional(TypeAdapter(HrvDailySummary)),
    lambda page: (page.hrv_summaries or []) if page else [],
)
_NUTRITION = _parser(TypeAdapter(DailyNutritionSummary), DailyNutritionSummary)
_MENSTRUAL_SUMMARY = _optional(TypeAdapter(MenstrualCycleSummary))
_MENSTRUAL_DAY = _optional(TypeAdapter(DetailedMenstrualCycleDay))
_SLEEP = _parser(TypeAdapter(DailySleep), DailySleep)
_SLEEP_SUMMARIES = _parser(TypeAdapter(list[DailySleep]), list)
_USER_SETTINGS = _parser(TypeAdapter(UserSettings), UserSettings)
_HYDRATION = _parser(TypeAdapter(list[UserDailyHydration]), list)
_STEPS = _parser(TypeAdapter(list[StepsDailyStat]), list)
_STRESS = _parser(TypeAdapter(list[MultiValueWellnessChartItem]), list)
_DAILY_SUMMARY = _parser(TypeAdapter(UserDailySummary), UserDailySummary)
_DAILY_SUMMARIES = _then(
    _parser(TypeAdapter(UserDailySummaryV2List), UserDailySummaryV2List),
    lambda page: page.data or [],
)
_WEIGHTS = _parser(TypeAdapter(list[DateWeight]), list)
_WEIGHT_RANGE = _parser(TypeAdapter(WeightRange), WeightRange)
_BODY_BATTERY = _parser(TypeAdapter(list[BodyBatteryDailyReport]), list)
_SPO2 = _parser(TypeAdapter(DailySpO2AndAcclimation), DailySpO2AndAcclimation)
_HEART_RATE = _parser(TypeAdapter(DailyHeartRate), DailyHeartRate)
_WELLNESS_ACTIVITIES = _parser(TypeAdapter(list[WellnessActivitySummary]), list)


def _archive(response: httpx.Response) -> bytes:
    if not response.content:
        raise ResponseValidationError("The activity download was empty", response=response)
    return response.content


# --------------------------------------------------------------------------------------
# Parameter helpers
# --------------------------------------------------------------------------------------


def _iso(value: DateLike, name: str = "date") -> str:
    return to_date(value, name).isoformat()


def _bool(value: object) -> str:
    return "true" if value else "false"


def _activity_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"activity_id must be an int, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"activity_id must be positive, got {value}")
    return value


def _range_path(prefix: str, start: date, end: date) -> str:
    return f"{prefix}/{start.isoformat()}/{end.isoformat()}"


def _range_params(start: date, end: date) -> Params:
    return (("startDate", start.isoformat()), ("endDate", end.isoformat()))


# --------------------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------------------


class BoundingBox(NamedTuple):
    """Geographic box for filtering activities by start location, in decimal degrees."""

    north: float
    """Northern latitude boundary."""
    east: float
    """Eastern longitude boundary."""
    south: float
    """Southern latitude boundary."""
    west: float
    """Western longitude boundary."""


class ActivityFilters(TypedDict, total=False):
    """Filters accepted by ``search_activities`` and ``iter_activities``.

    All filters are optional and combined with AND. Distances are in meters,
    durations in seconds.
    """

    start_date: DateLike
    """Earliest local start date, inclusive."""
    end_date: DateLike
    """Latest local start date, inclusive."""
    activity_type: str
    """Parent activity type key, such as ``running`` or ``cycling`` (not a subtype)."""
    activity_subtype: str
    """Activity subtype key, such as ``trail_running``."""
    excluded_activity_subtypes: Sequence[str]
    """Subtype keys to exclude."""
    event_type: str
    """Event type key, such as ``race``, ``training``, ``recreation`` or ``uncategorized``."""
    search: str
    """Text to search for in activity names and descriptions."""
    favorite: bool
    """Only favorites (``True``) or only non-favorites (``False``)."""
    sort_by: str
    """Field to sort by, such as ``startLocal`` (default), ``distance`` or ``duration``."""
    sort_order: Literal["asc", "desc"]
    """Sort direction (default ``desc``)."""
    exclude_children: bool
    """Exclude child activities, such as legs of a multisport activity (server default ``True``)."""
    min_distance: int
    """Minimum distance in meters."""
    max_distance: int
    """Maximum distance in meters."""
    min_duration: int
    """Minimum timer duration in seconds."""
    max_duration: int
    """Maximum timer duration in seconds."""
    min_elapsed_duration: int
    """Minimum elapsed duration (including pauses) in seconds."""
    max_elapsed_duration: int
    """Maximum elapsed duration (including pauses) in seconds."""
    min_elevation: int
    """Minimum total elevation gain in meters."""
    max_elevation: int
    """Maximum total elevation gain in meters."""
    min_avg_power: int
    """Minimum average power in watts."""
    bounding_box: BoundingBox
    """Only activities that started inside this box."""
    lower_max_depth: int
    """Dives: minimum of the maximum depth, in centimeters."""
    upper_max_depth: int
    """Dives: maximum of the maximum depth, in centimeters."""
    deco_dive: bool
    """Dives: only decompression (``True``) or non-decompression (``False``) dives."""
    min_bottom_time: int
    """Dives: minimum bottom time in seconds."""
    max_bottom_time: int
    """Dives: maximum bottom time in seconds."""
    required_fields: Sequence[str]
    """Activity fields that must be non-null, such as ``avgPower``."""


_SCALAR_FILTERS: dict[str, str] = {
    "activity_type": "activityType",
    "activity_subtype": "activitySubType",
    "event_type": "eventType",
    "search": "search",
    "sort_by": "sortBy",
    "sort_order": "sortOrder",
    "min_distance": "minDistance",
    "max_distance": "maxDistance",
    "min_duration": "minDuration",
    "max_duration": "maxDuration",
    "min_elapsed_duration": "minElapsedDuration",
    "max_elapsed_duration": "maxElapsedDuration",
    "min_elevation": "minElevation",
    "max_elevation": "maxElevation",
    "min_avg_power": "minAvgPower",
    "lower_max_depth": "lowerMaxDepth",
    "upper_max_depth": "upperMaxDepth",
    "min_bottom_time": "minBottomTime",
    "max_bottom_time": "maxBottomTime",
}
_BOOL_FILTERS: dict[str, str] = {"exclude_children": "excludeChildren", "deco_dive": "decoDive"}
_LIST_FILTERS: dict[str, str] = {
    "excluded_activity_subtypes": "excludedActivitySubType",
    "required_fields": "requiredField",
}
_KNOWN_FILTERS = frozenset(ActivityFilters.__annotations__)


def _date_and_flag_params(filters: ActivityFilters) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    if (start_date := filters.get("start_date")) is not None:
        params.append(("startDate", _iso(start_date, "start_date")))
    if (end_date := filters.get("end_date")) is not None:
        params.append(("endDate", _iso(end_date, "end_date")))
    if (favorite := filters.get("favorite")) is not None:
        params.append(("favorite", "1" if favorite else "0"))
    if (box := filters.get("bounding_box")) is not None:
        north, east, south, west = box
        params += [
            ("upperLatitude", str(north)),
            ("rightLongitude", str(east)),
            ("lowerLatitude", str(south)),
            ("leftLongitude", str(west)),
        ]
    return params


def _list_params(filters: ActivityFilters) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    for name, key in _LIST_FILTERS.items():
        values = filters.get(name)
        if values is None:
            continue
        if isinstance(values, str) or not isinstance(values, Sequence):
            raise TypeError(f"{name} must be a sequence of strings")
        params += [(key, str(value)) for value in values]
    return params


def activity_filter_params(filters: ActivityFilters) -> Params:
    """Translate :class:`ActivityFilters` into query parameters.

    Raises:
        TypeError: An unknown filter name or a malformed value was passed.
    """
    unknown = set(filters) - _KNOWN_FILTERS
    if unknown:
        raise TypeError(f"unknown activity filter(s): {', '.join(sorted(unknown))}")
    params = _date_and_flag_params(filters)
    params += [
        (key, _bool(flag))
        for name, key in _BOOL_FILTERS.items()
        if (flag := filters.get(name)) is not None
    ]
    params += _list_params(filters)
    params += [
        (key, str(value))
        for name, key in _SCALAR_FILTERS.items()
        if (value := filters.get(name)) is not None
    ]
    return tuple(params)


def get_activity(activity_id: int) -> Request[ActivitySummary]:
    """``GET /activity-service/activity/{activityId}``."""
    return Request(f"/activity-service/activity/{_activity_id(activity_id)}", _ACTIVITY)


def get_activity_workouts(activity_id: int) -> Request[list[ActivityWorkout]]:
    """``GET /activity-service/activity/{activityId}/workouts``."""
    return Request(
        f"/activity-service/activity/{_activity_id(activity_id)}/workouts", _ACTIVITY_WORKOUTS
    )


def search_activities(start: int, limit: int, filters: Params) -> Request[list[SummarizedActivity]]:
    """``GET /activitylist-service/activities/search/activities`` (one page)."""
    return Request(
        "/activitylist-service/activities/search/activities",
        _ACTIVITIES,
        (("start", str(start)), ("limit", str(limit)), *filters),
    )


def download_activity_zip(activity_id: int) -> Request[bytes]:
    """``GET /download-service/files/activity/{activityId}`` (zip archive)."""
    return Request(
        f"/download-service/files/activity/{_activity_id(activity_id)}", _archive, accept=ZIP
    )


def download_activity(activity_id: int) -> Request[ActivityFile]:
    """Like :func:`download_activity_zip`, extracting the activity file."""
    activity_id = _activity_id(activity_id)

    def parse(response: httpx.Response) -> ActivityFile:
        try:
            return extract_activity_file(activity_id, _archive(response))
        except ValueError as exc:
            raise ResponseValidationError(
                f"Cannot read activity {activity_id} download: {exc}", response=response
            ) from exc

    return Request(f"/download-service/files/activity/{activity_id}", parse, accept=ZIP)


# --------------------------------------------------------------------------------------
# Health and wellness
# --------------------------------------------------------------------------------------


def get_heart_rate_zones(*, generate_defaults: bool) -> Request[list[HeartRateZones]]:
    """``GET /biometric-service/heartRateZones/v2``."""
    return Request(
        "/biometric-service/heartRateZones/v2",
        _HEART_RATE_ZONES,
        (("generateDefaults", _bool(generate_defaults)),),
    )


def get_fitness_age(day: DateLike, *, full_history_scan: bool) -> Request[FitnessAge]:
    """``GET /fitnessage-service/fitnessage/v2/{calendarDate}``.

    The spec documents an array; the server returns a single object.
    """
    return Request(
        f"/fitnessage-service/fitnessage/v2/{_iso(day, 'day')}",
        _FITNESS_AGE,
        (("fullHistoryScan", _bool(full_history_scan)),),
    )


def get_effective_weight_goals(start: date, end: date) -> Request[UserGoalsRange]:
    """``GET /goal-service/goal/user/effective/weightgoal/{startDate}/{endDate}``."""
    return Request(
        _range_path("/goal-service/goal/user/effective/weightgoal", start, end), _WEIGHT_GOALS
    )


def get_daily_hrv(day: DateLike) -> Request[HrvChart | None]:
    """``GET /hrv-service/hrv/{calendarDate}``; ``None`` on HTTP 204."""
    return Request(f"/hrv-service/hrv/{_iso(day, 'day')}", _HRV)


def get_daily_hrv_summaries(start: date, end: date) -> Request[list[HrvSummary]]:
    """``GET /hrv-service/hrv/daily/{fromDate}/{toDate}``, unwrapped."""
    return Request(_range_path("/hrv-service/hrv/daily", start, end), _HRV_SUMMARIES)


def get_daily_food_logs(
    day: DateLike,
    *,
    include_goals: bool,
    include_nutrition: bool,
    include_media: bool,
    meal_id: int | None,
) -> Request[DailyNutritionSummary]:
    """``GET /nutrition-service/food/logs/{date}``."""
    params: list[tuple[str, str]] = [
        ("includeGoals", _bool(include_goals)),
        ("includeNutrition", _bool(include_nutrition)),
        ("includeMedia", _bool(include_media)),
    ]
    if meal_id is not None:
        params.append(("mealId", str(meal_id)))
    return Request(f"/nutrition-service/food/logs/{_iso(day, 'day')}", _NUTRITION, tuple(params))


def get_menstrual_cycle_summary(day: DateLike) -> Request[MenstrualCycleSummary | None]:
    """``GET /periodichealth-service/menstrualcycle/summary/{calendarDate}``."""
    return Request(
        f"/periodichealth-service/menstrualcycle/summary/{_iso(day, 'day')}", _MENSTRUAL_SUMMARY
    )


def get_menstrual_cycle_day_view(
    day: DateLike,
    *,
    today: DateLike | None,
    reduced_response: bool,
    skip_baby_movement: bool,
    include_analysis: bool,
) -> Request[DetailedMenstrualCycleDay | None]:
    """``GET /periodichealth-service/menstrualcycle/dayview/{calendarDate}``."""
    params: list[tuple[str, str]] = []
    if today is not None:
        params.append(("todayDate", _iso(today, "today")))
    params += [
        ("reducedResponse", _bool(reduced_response)),
        ("skipbm", _bool(skip_baby_movement)),
        ("includeAnalysis", _bool(include_analysis)),
    ]
    return Request(
        f"/periodichealth-service/menstrualcycle/dayview/{_iso(day, 'day')}",
        _MENSTRUAL_DAY,
        tuple(params),
    )


def get_daily_sleep(day: DateLike) -> Request[DailySleep]:
    """``GET /sleep-service/sleep/dailySleep``."""
    return Request("/sleep-service/sleep/dailySleep", _SLEEP, (("date", _iso(day, "day")),))


def get_daily_sleep_summaries(start: date, end: date) -> Request[list[DailySleep]]:
    """``GET /sleep-service/sleep/dailySleepSummariesByDate`` (max 31 days)."""
    return Request(
        "/sleep-service/sleep/dailySleepSummariesByDate",
        _SLEEP_SUMMARIES,
        _range_params(start, end),
    )


def get_user_profile_settings() -> Request[UserSettings]:
    """``GET /userprofile-service/userprofile/settings``."""
    return Request("/userprofile-service/userprofile/settings", _USER_SETTINGS)


def get_daily_hydration(start: date, end: date) -> Request[list[UserDailyHydration]]:
    """``GET /usersummary-service/stats/hydration/daily/{startDate}/{endDate}`` (max 31 days)."""
    return Request(
        _range_path("/usersummary-service/stats/hydration/daily", start, end), _HYDRATION
    )


def get_daily_steps(start: date, end: date) -> Request[list[StepsDailyStat]]:
    """``GET /usersummary-service/stats/steps/daily/{startDate}/{endDate}`` (max 28 days)."""
    return Request(_range_path("/usersummary-service/stats/steps/daily", start, end), _STEPS)


def get_daily_stress(start: date, end: date) -> Request[list[MultiValueWellnessChartItem]]:
    """``GET /usersummary-service/stats/stress/daily/{startDate}/{endDate}`` (max 28 days)."""
    return Request(_range_path("/usersummary-service/stats/stress/daily", start, end), _STRESS)


def get_daily_summary(day: DateLike) -> Request[UserDailySummary]:
    """``GET /usersummary-service/usersummary/daily``."""
    return Request(
        "/usersummary-service/usersummary/daily",
        _DAILY_SUMMARY,
        (("calendarDate", _iso(day, "day")),),
    )


def get_daily_summaries(start: date, end: date) -> Request[list[UserDailySummaryV2]]:
    """``GET /usersummary-service/v2/dailysummaries``, unwrapped."""
    return Request(
        "/usersummary-service/v2/dailysummaries", _DAILY_SUMMARIES, _range_params(start, end)
    )


def get_weight_entries(start: date, end: date) -> Request[list[DateWeight]]:
    """``GET /weight-service/weight/all/{startDate}/{endDate}``."""
    return Request(_range_path("/weight-service/weight/all", start, end), _WEIGHTS)


def get_weight_range(start: date, end: date, *, include_all: bool) -> Request[WeightRange]:
    """``GET /weight-service/weight/range/{startDate}/{endDate}``."""
    return Request(
        _range_path("/weight-service/weight/range", start, end),
        _WEIGHT_RANGE,
        (("includeAll", _bool(include_all)),),
    )


def get_daily_body_battery(
    start: date, end: date, *, chunks: int | None
) -> Request[list[BodyBatteryDailyReport]]:
    """``GET /wellness-service/wellness/bodyBattery/reports/daily`` (max 31 days)."""
    params = _range_params(start, end)
    if chunks is not None:
        if chunks < 1:
            raise ValueError("chunks must be at least 1")
        params += (("chunks", str(chunks)),)
    return Request("/wellness-service/wellness/bodyBattery/reports/daily", _BODY_BATTERY, params)


def get_daily_spo2(day: DateLike) -> Request[DailySpO2AndAcclimation]:
    """``GET /wellness-service/wellness/daily/spo2acclimation/{calendarDate}``."""
    return Request(f"/wellness-service/wellness/daily/spo2acclimation/{_iso(day, 'day')}", _SPO2)


def get_daily_heart_rate(day: DateLike) -> Request[DailyHeartRate]:
    """``GET /wellness-service/wellness/dailyHeartRate``."""
    return Request(
        "/wellness-service/wellness/dailyHeartRate", _HEART_RATE, (("date", _iso(day, "day")),)
    )


def get_wellness_activity_summaries(
    start: date, end: date
) -> Request[list[WellnessActivitySummary]]:
    """``GET /wellnessactivity-service/activity/summary/range/{fromDate}/{untilDate}``."""
    return Request(
        _range_path("/wellnessactivity-service/activity/summary/range", start, end),
        _WELLNESS_ACTIVITIES,
    )


def get_json(path: str, params: Sequence[tuple[str, str]] = ()) -> Request[JsonValue]:
    """Any GET endpoint on the API host, returning decoded JSON (or ``None`` if empty)."""
    if not path.startswith("/") or path.startswith("//") or "://" in path:
        raise ValueError(
            "path must be an absolute path on the API host, such as '/sleep-service/...'"
        )
    return Request(path, _json, tuple(params))
