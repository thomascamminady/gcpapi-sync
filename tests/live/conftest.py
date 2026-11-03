"""Outages at Garmin are not changes of the API.

When Garmin's server answers with an error of its own (HTTP 5xx) even after the client's
retries, the endpoint is down, which the client can do nothing about. Such a test is skipped
with the endpoint and the status in its reason, which the drift job turns into a warning,
instead of failing as if the API had changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from gcpapi_sync import InternalServerError

if TYPE_CHECKING:
    from collections.abc import Generator

OUTAGE = "Garmin's server answered HTTP"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    try:
        return (yield)
    except InternalServerError as error:
        pytest.skip(
            f"{item.name}: {OUTAGE} {error.status_code}, an outage on Garmin's side, "
            "not a change of the API"
        )
