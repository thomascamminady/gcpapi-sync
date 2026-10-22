"""Behaviour shared by GarminClient and AsyncGarminClient (each test runs on both)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from gcpapi_sync import (
    ActivityFile,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConfigurationError,
    FileTokenCache,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ResponseValidationError,
    RetryConfig,
    models,
)
from tests.cases import ACTIVITY_ID, CASES, Case
from tests.helpers import TOKEN_PATH, FakeGarmin, Harness, Reply, make_jwt, open_harness

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue

SETTINGS = "/userprofile-service/userprofile/settings"
SEARCH = "/activitylist-service/activities/search/activities"


def params_of(request: httpx.Request) -> tuple[tuple[str, str], ...]:
    return tuple(request.url.params.multi_items())


# -- every endpoint ------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_endpoint(harness: Harness, case: Case) -> None:
    harness.fake.reply(case.path, case.reply)

    result = harness.call(case.method, *case.args, **case.kwargs)

    assert isinstance(result, case.result)
    if case.item is not None:
        assert isinstance(result, list)
        assert result
        assert all(isinstance(item, case.item) for item in result)
    (request,) = harness.fake.data_requests
    assert request.method == "GET"
    assert request.url.path == case.path
    assert params_of(request) == case.params
    assert request.headers["Accept"] == case.accept


def test_download_activity_extracts_the_file(harness: Harness) -> None:
    harness.fake.reply(f"/download-service/files/activity/{ACTIVITY_ID}", CASES[3].reply)

    result = harness.call("download_activity", ACTIVITY_ID)

    assert isinstance(result, ActivityFile)
    assert result.filename == f"{ACTIVITY_ID}_ACTIVITY.fit"
    assert result.extension == "fit"
    assert result.content == b"\x0e\x10.FIT-data"


def test_unwrapped_endpoints_return_empty_lists_without_data(harness: Harness) -> None:
    harness.fake.reply("/hrv-service/hrv/daily/2026-10-01/2026-10-02", Reply(status=204))
    harness.fake.reply("/usersummary-service/v2/dailysummaries", Reply.json({"data": None}))

    assert harness.call("get_daily_hrv_summaries", "2026-10-01", "2026-10-02") == []
    assert harness.call("get_daily_summaries", "2026-10-01", "2026-10-02") == []


def test_empty_object_response_yields_empty_model(harness: Harness) -> None:
    harness.fake.reply(SETTINGS, Reply(status=200))

    result = harness.call("get_user_profile_settings")

    assert result == models.UserSettings()


def test_empty_activity_is_an_error(harness: Harness) -> None:
    harness.fake.reply(f"/activity-service/activity/{ACTIVITY_ID}", Reply(status=204))

    with pytest.raises(ResponseValidationError, match="empty response"):
        harness.call("get_activity", ACTIVITY_ID)


def test_invalid_payload_raises_response_validation_error(harness: Harness) -> None:
    harness.fake.reply("/sleep-service/sleep/dailySleep", Reply.json({"sleepTimeSeconds": "long"}))

    with pytest.raises(ResponseValidationError) as raised:
        harness.call("get_daily_sleep", "2026-10-21")

    assert "sleepTimeSeconds" in str(raised.value)
    assert raised.value.raw == b'{"sleepTimeSeconds": "long"}'
    assert raised.value.validation_error is not None


# -- date ranges ---------------------------------------------------------------------------


def window_reply(request: httpx.Request) -> Reply:
    """Echo the requested window as one steps record per window."""
    _, first, last = request.url.path.rsplit("/", 2)
    return Reply.json(
        [
            {
                "calendarDate": first,
                "totalSteps": (date.fromisoformat(last) - date.fromisoformat(first)).days + 1,
            }
        ]
    )


def test_long_ranges_are_split_into_windows(harness: Harness) -> None:
    harness.fake.respond_with("/usersummary-service/stats/steps/daily/", window_reply)

    result = harness.call("get_daily_steps", "2026-01-01", "2026-03-01")

    assert isinstance(result, list)
    assert [
        (item.calendar_date, item.total_steps)
        for item in result
        if isinstance(item, models.StepsDailyStat)
    ] == [
        (date(2026, 1, 1), 28),
        (date(2026, 1, 29), 28),
        (date(2026, 2, 26), 4),
    ]
    paths = sorted(request.url.path for request in harness.fake.data_requests)
    assert paths == [
        "/usersummary-service/stats/steps/daily/2026-01-01/2026-01-28",
        "/usersummary-service/stats/steps/daily/2026-01-29/2026-02-25",
        "/usersummary-service/stats/steps/daily/2026-02-26/2026-03-01",
    ]


def test_query_parameter_ranges_are_split(harness: Harness) -> None:
    def reply(request: httpx.Request) -> Reply:
        return Reply.json([{"calendarDate": request.url.params["startDate"]}])

    harness.fake.respond_with("/sleep-service/sleep/dailySleepSummariesByDate", reply)

    result = harness.call("get_daily_sleep_summaries", date(2026, 1, 1), date(2026, 2, 15))

    assert isinstance(result, list)
    assert [item.calendar_date for item in result if isinstance(item, models.DailySleep)] == [
        date(2026, 1, 1),
        date(2026, 2, 1),
    ]


@pytest.mark.parametrize(
    ("method", "days", "requests"),
    [
        ("get_daily_steps", 28, 1),
        ("get_daily_steps", 29, 2),
        ("get_daily_stress", 56, 2),
        ("get_daily_hydration", 31, 1),
        ("get_daily_hydration", 32, 2),
        ("get_daily_body_battery", 62, 2),
        ("get_daily_sleep_summaries", 93, 3),
        ("get_daily_summaries", 32, 2),
        ("get_daily_hrv_summaries", 366, 2),
    ],
)
def test_window_sizes(harness: Harness, method: str, days: int, requests: int) -> None:
    def empty(request: httpx.Request) -> Reply:
        if "dailysummaries" in request.url.path:
            return Reply.json({"data": []})
        if request.url.path.startswith("/hrv-service"):
            return Reply.json({"hrvSummaries": []})
        return Reply.json([])

    harness.fake.respond_with("/", empty)
    end = date(2026, 10, 21)

    harness.call(method, end - timedelta(days=days - 1), end)

    assert len(harness.fake.data_requests) == requests


def test_reversed_range_is_rejected_before_any_request(harness: Harness) -> None:
    with pytest.raises(ValueError, match="must not be after"):
        harness.call("get_daily_steps", "2026-10-21", "2026-10-01")
    assert harness.fake.requests == []


def test_malformed_dates_are_rejected(harness: Harness) -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        harness.call("get_daily_sleep", "21.10.2026")
    with pytest.raises(TypeError, match=r"datetime\.date"):
        harness.call("get_daily_sleep", 20261021)


# -- activity search and pagination --------------------------------------------------------


def activities(count: int, offset: int = 0) -> list[JsonValue]:
    return [{"activityId": offset + index} for index in range(count)]


def test_search_pages_large_limits(harness: Harness) -> None:
    def reply(request: httpx.Request) -> Reply:
        start, limit = int(request.url.params["start"]), int(request.url.params["limit"])
        return Reply.json(activities(min(limit, max(0, 100 - start)), start))

    harness.fake.respond_with(SEARCH, reply)

    result = harness.call("search_activities", limit=45, activity_type="running")

    assert isinstance(result, list)
    assert len(result) == 45
    assert [(r.url.params["start"], r.url.params["limit"]) for r in harness.fake.data_requests] == [
        ("0", "20"),
        ("20", "20"),
        ("40", "5"),
    ]
    assert all(r.url.params["activityType"] == "running" for r in harness.fake.data_requests)


def test_search_stops_at_the_last_page(harness: Harness) -> None:
    harness.fake.reply(SEARCH, Reply.json(activities(20)), Reply.json(activities(3, 20)))

    result = harness.call("search_activities", start=0, limit=100)

    assert isinstance(result, list)
    assert len(result) == 23
    assert len(harness.fake.data_requests) == 2


def test_search_with_zero_limit_makes_no_request(harness: Harness) -> None:
    assert harness.call("search_activities", limit=0) == []
    assert harness.fake.requests == []


def test_search_rejects_negative_arguments(harness: Harness) -> None:
    with pytest.raises(ValueError, match=">= 0"):
        harness.call("search_activities", start=-1)


def test_search_rejects_unknown_filters(harness: Harness) -> None:
    with pytest.raises(TypeError, match="unknown activity filter"):
        harness.call("search_activities", activity_kind="running")


@pytest.mark.parametrize(("total", "expected_requests"), [(5, 3), (4, 3), (0, 1)])
def test_iter_activities_fetches_lazily_until_a_short_page(
    harness: Harness, total: int, expected_requests: int
) -> None:
    def reply(request: httpx.Request) -> Reply:
        start, limit = int(request.url.params["start"]), int(request.url.params["limit"])
        return Reply.json(activities(min(limit, max(0, total - start)), start))

    harness.fake.respond_with(SEARCH, reply)

    result = harness.call("iter_activities", page_size=2, start_date="2026-10-01")

    assert isinstance(result, list)
    assert [
        item.activity_id for item in result if isinstance(item, models.SummarizedActivity)
    ] == list(range(total))
    assert len(harness.fake.data_requests) == expected_requests
    assert all(r.url.params["startDate"] == "2026-10-01" for r in harness.fake.data_requests)


@pytest.mark.parametrize(("page_size", "start"), [(0, 0), (46, 0), (10, -1)])
def test_iter_activities_validates_arguments(harness: Harness, page_size: int, start: int) -> None:
    with pytest.raises(ValueError, match="must be"):
        harness.call("iter_activities", page_size=page_size, start=start)


# -- authentication ------------------------------------------------------------------------


def test_token_is_exchanged_once_and_reused(harness: Harness) -> None:
    harness.fake.reply(SETTINGS, Reply.json({}))

    for _ in range(3):
        harness.call("get_user_profile_settings")

    (exchange,) = harness.fake.token_requests
    assert exchange.method == "POST"
    assert exchange.headers["Authorization"] == "ApiKey test-api-key"
    token = harness.fake.issued[0]
    assert all(r.headers["Authorization"] == f"Bearer {token}" for r in harness.fake.data_requests)
    assert all(r.headers["User-Agent"].startswith("gcpapi-sync/") for r in harness.fake.requests)


def test_rejected_token_is_renewed_once(harness: Harness) -> None:
    harness.fake.reply(SETTINGS, Reply.json({}))
    harness.call("get_user_profile_settings")
    harness.fake.revoke_all()

    harness.call("get_user_profile_settings")

    assert len(harness.fake.token_requests) == 2
    assert [r.headers["Authorization"] for r in harness.fake.data_requests] == [
        f"Bearer {harness.fake.issued[0]}",
        f"Bearer {harness.fake.issued[0]}",
        f"Bearer {harness.fake.issued[1]}",
    ]


def test_persistent_401_raises_authentication_error(harness: Harness) -> None:
    harness.fake.reply(SETTINGS, Reply(status=401))

    with pytest.raises(AuthenticationError, match="401"):
        harness.call("get_user_profile_settings")
    assert len(harness.fake.token_requests) == 2


def test_expiring_token_is_renewed_before_use(harness: Harness) -> None:
    harness.fake.token_lifetime = timedelta(minutes=2)
    harness.fake.reply(SETTINGS, Reply.json({}))

    harness.call("get_user_profile_settings")
    harness.call("get_user_profile_settings")

    assert len(harness.fake.token_requests) == 2


def test_force_refresh(harness: Harness) -> None:
    first = harness.call("get_access_token")
    cached = harness.call("get_access_token")
    renewed = harness.call("get_access_token", force_refresh=True)

    assert first == cached
    assert renewed != first
    assert len(harness.fake.token_requests) == 2


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_rejected_api_key(fake: FakeGarmin, kind: str) -> None:
    with (
        open_harness("sync" if kind == "sync" else "async", fake, api_key="wrong") as harness,
        pytest.raises(AuthenticationError, match="rejected the API key"),
    ):
        harness.call("get_user_profile_settings")
    assert fake.data_requests == []


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_access_token_without_key(fake: FakeGarmin, kind: str) -> None:
    token = make_jwt()
    fake.issued.append(token)
    fake.reply(SETTINGS, Reply.json({}))
    with open_harness(
        "sync" if kind == "sync" else "async", fake, api_key=None, access_token=token
    ) as harness:
        harness.call("get_user_profile_settings")
        assert fake.token_requests == []
        fake.revoke_all()
        with pytest.raises(AuthenticationError):
            harness.call("get_user_profile_settings")
        with pytest.raises(ConfigurationError, match="no API key"):
            harness.call("get_access_token", force_refresh=True)


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_access_token_with_key_renews_when_rejected(fake: FakeGarmin, kind: str) -> None:
    fake.reply(SETTINGS, Reply.json({}))
    with open_harness(
        "sync" if kind == "sync" else "async", fake, access_token=make_jwt()
    ) as harness:
        harness.call("get_user_profile_settings")
    assert len(fake.token_requests) == 1


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_file_token_cache_is_shared(fake: FakeGarmin, kind: str, tmp_path: Path) -> None:
    fake.reply(SETTINGS, Reply.json({}))
    cache = FileTokenCache(tmp_path / "token.json")
    for _ in range(2):
        with open_harness(
            "sync" if kind == "sync" else "async", fake, token_cache=cache
        ) as harness:
            harness.call("get_user_profile_settings")
    assert len(fake.token_requests) == 1


@pytest.mark.parametrize(
    ("body", "expected"), [(b'"quoted.jwt.token"', "quoted.jwt.token"), (b"  plain  ", "plain")]
)
def test_token_body_is_normalized(harness: Harness, body: bytes, expected: str) -> None:
    harness.fake.token_replies.append(Reply(body=body))

    token = harness.call("get_access_token")

    assert getattr(token, "value", None) == expected


def test_empty_token_body(harness: Harness) -> None:
    harness.fake.token_replies.append(Reply(body=b""))

    with pytest.raises(ResponseValidationError, match="empty body"):
        harness.call("get_access_token")


def test_token_exchange_is_retried(harness: Harness) -> None:
    harness.fake.token_replies.extend([Reply(status=503), Reply(body=make_jwt().encode())])

    harness.call("get_access_token")

    assert len(harness.fake.token_requests) == 2


def test_token_exchange_server_error(harness: Harness) -> None:
    harness.fake.token_replies.append(Reply(status=500))

    with pytest.raises(InternalServerError):
        harness.call("get_access_token")


# -- retries and errors --------------------------------------------------------------------


def test_transient_status_is_retried(harness: Harness) -> None:
    harness.fake.reply(
        SETTINGS,
        Reply(status=503),
        Reply(status=429, headers=(("Retry-After", "0"),)),
        Reply.json({}),
    )

    harness.call("get_user_profile_settings")

    assert len(harness.fake.data_requests) == 3


def test_retries_are_bounded(harness: Harness) -> None:
    harness.fake.reply(SETTINGS, Reply(status=500))

    with pytest.raises(InternalServerError):
        harness.call("get_user_profile_settings")

    assert len(harness.fake.data_requests) == 3


def test_long_retry_after_is_not_waited_for(harness: Harness) -> None:
    harness.fake.reply(SETTINGS, Reply(status=429, headers=(("Retry-After", "3600"),)))

    with pytest.raises(RateLimitError) as raised:
        harness.call("get_user_profile_settings")

    assert raised.value.retry_after == 3600
    assert len(harness.fake.data_requests) == 1


def test_client_errors_are_not_retried(harness: Harness) -> None:
    harness.fake.reply(SETTINGS, Reply(status=400, body=b"{}"))

    with pytest.raises(BadRequestError, match="Garmin rejected the parameters"):
        harness.call("get_user_profile_settings")

    assert len(harness.fake.data_requests) == 1


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (418, APIStatusError),
        (502, InternalServerError),
    ],
)
def test_status_errors(harness: Harness, status: int, error: type[APIStatusError]) -> None:
    harness.fake.reply(SETTINGS, Reply(status=status))

    with pytest.raises(error) as raised:
        harness.call("get_user_profile_settings")

    assert raised.value.status_code == status
    assert raised.value.request is not None


def test_error_body_details(harness: Harness) -> None:
    body: JsonValue = {
        "clientMessage": "Reference Error ID",
        "errorId": "abc-123",
        "error": "NotAcceptableException",
    }
    harness.fake.reply(SETTINGS, Reply.json(body, status=406))

    with pytest.raises(APIStatusError) as raised:
        harness.call("get_user_profile_settings")

    assert raised.value.error_id == "abc-123"
    assert raised.value.error is not None
    assert raised.value.error.error == "NotAcceptableException"
    assert raised.value.body == body
    assert "Reference Error ID (errorId abc-123)" in str(raised.value)


def test_non_json_error_body(harness: Harness) -> None:
    harness.fake.reply(SETTINGS, Reply(status=502, body=b"<html>Bad gateway</html>"))

    with pytest.raises(InternalServerError) as raised:
        harness.call("get_user_profile_settings")

    assert raised.value.body == "<html>Bad gateway</html>"
    assert raised.value.error is None
    assert raised.value.error_id is None
    assert "Bad gateway" in str(raised.value)


def test_transport_errors_are_retried(harness: Harness) -> None:
    harness.fake.reply(SETTINGS, Reply(error=httpx.ConnectError), Reply.json({}))

    harness.call("get_user_profile_settings")

    assert len(harness.fake.data_requests) == 2


@pytest.mark.parametrize(
    ("failure", "error"),
    [(httpx.ConnectError, APIConnectionError), (httpx.ReadTimeout, APITimeoutError)],
)
def test_persistent_transport_errors(
    harness: Harness, failure: type[httpx.TransportError], error: type[APIConnectionError]
) -> None:
    harness.fake.reply(SETTINGS, Reply(error=failure))

    with pytest.raises(error, match="simulated failure"):
        harness.call("get_user_profile_settings")

    assert len(harness.fake.data_requests) == 3


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_retries_can_be_disabled(fake: FakeGarmin, kind: str) -> None:
    fake.reply(SETTINGS, Reply(status=503))
    with (
        open_harness(
            "sync" if kind == "sync" else "async", fake, retry=RetryConfig(max_retries=0)
        ) as harness,
        pytest.raises(InternalServerError),
    ):
        harness.call("get_user_profile_settings")
    assert len(fake.data_requests) == 1


def test_token_path_constant_matches_the_client() -> None:
    from gcpapi_sync._constants import KEY_EXCHANGE_PATH

    assert KEY_EXCHANGE_PATH == TOKEN_PATH


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"errorId": "only-an-id"}', '{"errorId": "only-an-id"}'),
        (b'{"error": 123}', '{"error": 123}'),
        (b"[1, 2]", "[1, 2]"),
    ],
)
def test_json_error_bodies_without_a_message(harness: Harness, body: bytes, expected: str) -> None:
    harness.fake.reply(SETTINGS, Reply(status=409, body=body))

    with pytest.raises(APIStatusError) as raised:
        harness.call("get_user_profile_settings")

    assert str(raised.value).endswith(f"returned HTTP 409: {expected}")
