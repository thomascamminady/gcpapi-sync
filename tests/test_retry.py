from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from gcpapi_sync import RetryConfig
from gcpapi_sync._retry import parse_retry_after

if TYPE_CHECKING:
    from collections.abc import Callable


def test_backoff_doubles_and_is_capped() -> None:
    config = RetryConfig(initial_delay=0.5, max_delay=3, jitter=0)

    assert [config.backoff(attempt) for attempt in range(5)] == [0.5, 1.0, 2.0, 3.0, 3.0]


def test_backoff_jitter_stays_within_bounds() -> None:
    config = RetryConfig(initial_delay=1, max_delay=10, jitter=0.25)

    delays = {config.backoff(0) for _ in range(200)}

    assert all(0.75 <= delay <= 1.25 for delay in delays)
    assert len(delays) > 1


@pytest.mark.parametrize(
    "make",
    [
        lambda: RetryConfig(max_retries=-1),
        lambda: RetryConfig(initial_delay=-1),
        lambda: RetryConfig(max_delay=-1),
        lambda: RetryConfig(max_retry_after=-1),
        lambda: RetryConfig(jitter=1.5),
        lambda: RetryConfig(jitter=-0.1),
    ],
)
def test_invalid_configuration(make: Callable[[], RetryConfig]) -> None:
    with pytest.raises(ValueError, match=r">= 0|between 0 and 1"):
        make()


def test_defaults_retry_transient_statuses_only() -> None:
    statuses = RetryConfig().retry_statuses

    assert {429, 500, 502, 503, 504} <= statuses
    assert not statuses & {400, 401, 403, 404}


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("5", 5.0),
        (" 2.5 ", 2.5),
        ("-3", 0.0),
        ("not a date", None),
        ("Thu, 22 Oct 2026 06:00:30 GMT", 30.0),
        ("Thu, 22 Oct 2026 05:00:00 GMT", 0.0),
        ("Thu, 22 Oct 2026 06:00:30 -0000", 30.0),
    ],
)
def test_parse_retry_after(header: str | None, expected: float | None) -> None:
    now = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)

    assert parse_retry_after(header, now=now) == expected


def test_parse_retry_after_defaults_to_the_current_time() -> None:
    assert parse_retry_after("Mon, 01 Jan 2001 00:00:00 GMT") == 0.0
