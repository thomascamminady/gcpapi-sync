from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest

from gcpapi_sync._dates import to_date, to_range, windows


def test_to_date_accepts_dates_datetimes_and_iso_strings() -> None:
    assert to_date(date(2026, 10, 21)) == date(2026, 10, 21)
    assert to_date(datetime(2026, 10, 21, 23, 59, tzinfo=UTC)) == date(2026, 10, 21)
    assert to_date("2026-10-21") == date(2026, 10, 21)


@pytest.mark.parametrize("value", ["2026/10/21", "21.10.2026", "", "yesterday"])
def test_to_date_rejects_malformed_strings(value: str) -> None:
    with pytest.raises(ValueError, match="day must be a YYYY-MM-DD date"):
        to_date(value, "day")


@pytest.mark.parametrize("value", [20261021, None, 1.5, ["2026-10-21"]])
def test_to_date_rejects_other_types(value: object) -> None:
    with pytest.raises(TypeError, match=r"must be a datetime\.date"):
        to_date(value)


def test_to_range() -> None:
    assert to_range("2026-10-01", date(2026, 10, 1)) == (date(2026, 10, 1), date(2026, 10, 1))
    with pytest.raises(ValueError, match="must not be after"):
        to_range("2026-10-02", "2026-10-01")


def test_windows_examples() -> None:
    assert windows(date(2026, 1, 1), date(2026, 1, 1), 28) == [(date(2026, 1, 1), date(2026, 1, 1))]
    assert windows(date(2026, 1, 1), date(2026, 1, 3), 2) == [
        (date(2026, 1, 1), date(2026, 1, 2)),
        (date(2026, 1, 3), date(2026, 1, 3)),
    ]


@pytest.mark.parametrize("max_days", [1, 7, 28, 31, 365])
@pytest.mark.parametrize("length", [1, 2, 27, 28, 29, 31, 32, 365, 366, 1000])
def test_windows_partition_the_range(max_days: int, length: int) -> None:
    start = date(2024, 2, 20)
    end = start + timedelta(days=length - 1)

    spans = windows(start, end, max_days)

    assert spans[0][0] == start
    assert spans[-1][1] == end
    for (_, previous_end), (next_start, _) in pairwise(spans):
        assert next_start == previous_end + timedelta(days=1)
    assert all(0 < (last - first).days + 1 <= max_days for first, last in spans)
    assert len(spans) == -(-length // max_days)


def test_windows_rejects_non_positive_sizes() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        windows(date(2026, 1, 1), date(2026, 1, 2), 0)
