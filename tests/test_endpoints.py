from __future__ import annotations

from datetime import date
from typing import cast

import httpx
import pytest

from gcpapi_sync import ActivityFilters, BoundingBox, ResponseValidationError
from gcpapi_sync import _endpoints as ep
from tests.helpers import zip_archive


def untyped(filters: dict[str, object]) -> ActivityFilters:
    """Filters as an untyped caller might pass them."""
    return cast("ActivityFilters", filters)


def response(content: bytes, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=content, request=httpx.Request("GET", "https://garmin.test/x")
    )


def test_every_activity_filter_is_translated() -> None:
    filters: ActivityFilters = {
        "start_date": date(2026, 1, 1),
        "end_date": "2026-01-31",
        "activity_type": "running",
        "activity_subtype": "trail_running",
        "excluded_activity_subtypes": ["treadmill_running", "track_running"],
        "event_type": "race",
        "search": "marathon",
        "favorite": True,
        "sort_by": "distance",
        "sort_order": "asc",
        "exclude_children": False,
        "min_distance": 1000,
        "max_distance": 42195,
        "min_duration": 60,
        "max_duration": 7200,
        "min_elapsed_duration": 61,
        "max_elapsed_duration": 7201,
        "min_elevation": 10,
        "max_elevation": 2000,
        "min_avg_power": 200,
        "bounding_box": BoundingBox(north=51.0, east=7.5, south=50.5, west=6.5),
        "lower_max_depth": 100,
        "upper_max_depth": 3000,
        "deco_dive": True,
        "min_bottom_time": 30,
        "max_bottom_time": 3600,
        "required_fields": ["avgPower"],
    }

    params = dict(ep.activity_filter_params(filters))
    repeated = ep.activity_filter_params(filters)

    assert params == {
        "startDate": "2026-01-01",
        "endDate": "2026-01-31",
        "activityType": "running",
        "activitySubType": "trail_running",
        "excludedActivitySubType": "track_running",
        "eventType": "race",
        "search": "marathon",
        "favorite": "1",
        "sortBy": "distance",
        "sortOrder": "asc",
        "excludeChildren": "false",
        "minDistance": "1000",
        "maxDistance": "42195",
        "minDuration": "60",
        "maxDuration": "7200",
        "minElapsedDuration": "61",
        "maxElapsedDuration": "7201",
        "minElevation": "10",
        "maxElevation": "2000",
        "minAvgPower": "200",
        "upperLatitude": "51.0",
        "rightLongitude": "7.5",
        "lowerLatitude": "50.5",
        "leftLongitude": "6.5",
        "lowerMaxDepth": "100",
        "upperMaxDepth": "3000",
        "decoDive": "true",
        "minBottomTime": "30",
        "maxBottomTime": "3600",
        "requiredField": "avgPower",
    }
    assert [value for key, value in repeated if key == "excludedActivitySubType"] == [
        "treadmill_running",
        "track_running",
    ]
    assert ep.activity_filter_params({}) == ()


def test_favorite_false_and_plain_tuple_bounding_box() -> None:
    filters: ActivityFilters = {"favorite": False, "bounding_box": BoundingBox(1, 2, 3, 4)}

    assert ep.activity_filter_params(filters)[0] == ("favorite", "0")


def test_unknown_filters_are_rejected() -> None:
    with pytest.raises(TypeError, match=r"unknown activity filter.*activity_kind"):
        ep.activity_filter_params(untyped({"activity_kind": "x"}))


def test_list_filters_reject_plain_strings() -> None:
    with pytest.raises(TypeError, match="sequence of strings"):
        ep.activity_filter_params(untyped({"required_fields": "avgPower"}))
    with pytest.raises(TypeError, match="sequence of strings"):
        ep.activity_filter_params(untyped({"required_fields": 3}))


def test_none_filter_values_are_skipped() -> None:
    assert ep.activity_filter_params(untyped({"search": None, "favorite": None})) == ()


@pytest.mark.parametrize("value", [0, -5])
def test_activity_ids_must_be_positive(value: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        ep.get_activity(value)


@pytest.mark.parametrize("value", [True, "123", 1.0])
def test_activity_ids_must_be_ints(value: object) -> None:
    with pytest.raises(TypeError, match="must be an int"):
        ep.get_activity(cast("int", value))


@pytest.mark.parametrize(
    "path", ["sleep", "//evil.example/x", "https://evil.example/x", "/x?u=https://e"]
)
def test_get_json_only_allows_paths_on_the_api_host(path: str) -> None:
    with pytest.raises(ValueError, match="absolute path"):
        ep.get_json(path)


def test_body_battery_chunks_must_be_positive() -> None:
    with pytest.raises(ValueError, match="chunks"):
        ep.get_daily_body_battery(date(2026, 1, 1), date(2026, 1, 2), chunks=0)


def test_menstrual_day_view_without_today() -> None:
    request = ep.get_menstrual_cycle_day_view(
        "2026-10-21",
        today=None,
        reduced_response=True,
        skip_baby_movement=True,
        include_analysis=False,
    )

    assert request.params == (
        ("reducedResponse", "true"),
        ("skipbm", "true"),
        ("includeAnalysis", "false"),
    )


def test_food_logs_without_meal() -> None:
    request = ep.get_daily_food_logs(
        "2026-10-21",
        include_goals=False,
        include_nutrition=False,
        include_media=False,
        meal_id=None,
    )

    assert ("mealId", "3") not in request.params
    assert dict(request.params)["includeGoals"] == "false"


def test_download_parsers() -> None:
    archive = zip_archive({"5_ACTIVITY.fit": b"fit"})

    assert ep.download_activity_zip(5).parse(response(archive)) == archive
    assert ep.download_activity(5).parse(response(archive)).content == b"fit"
    with pytest.raises(ResponseValidationError, match="empty"):
        ep.download_activity_zip(5).parse(response(b""))
    with pytest.raises(ResponseValidationError, match="not a zip"):
        ep.download_activity(5).parse(response(b"<html/>"))


def test_get_json_parser() -> None:
    parse = ep.get_json("/x").parse

    assert parse(response(b'{"a": [1, null]}')) == {"a": [1, None]}
    assert parse(response(b"", status=204)) is None
    with pytest.raises(ResponseValidationError):
        parse(response(b"not json"))
