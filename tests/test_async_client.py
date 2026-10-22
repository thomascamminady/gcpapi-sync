"""Behaviour specific to AsyncGarminClient."""

from __future__ import annotations

import anyio
import httpx
import pytest

from gcpapi_sync import AsyncGarminClient, NotFoundError
from gcpapi_sync._compat import override
from tests.helpers import API_KEY, BASE_URL, NO_WAIT, FakeGarmin, Reply

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class SlowTransport(httpx.AsyncBaseTransport):
    """Delays every response and records how many requests overlap."""

    def __init__(self, fake: FakeGarmin) -> None:
        self.fake = fake
        self.in_flight = 0
        self.peak = 0

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await anyio.sleep(0.01)
            return await self.fake.transport().handle_async_request(request)
        finally:
            self.in_flight -= 1


def steps_reply(request: httpx.Request) -> Reply:
    return Reply.json([{"calendarDate": request.url.path.rsplit("/", 2)[1]}])


@pytest.mark.parametrize("limit", [1, 3])
async def test_windows_are_fetched_concurrently_up_to_the_limit(
    fake: FakeGarmin, limit: int
) -> None:
    fake.respond_with("/usersummary-service/stats/steps/daily/", steps_reply)
    transport = SlowTransport(fake)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AsyncGarminClient(
            API_KEY, base_url=BASE_URL, retry=NO_WAIT, http_client=http, max_concurrency=limit
        )
        await client.get_access_token()

        result = await client.get_daily_steps("2026-01-01", "2026-06-30")

    assert transport.peak == limit
    assert [str(item.calendar_date) for item in result] == [
        "2026-01-01",
        "2026-01-29",
        "2026-02-26",
        "2026-03-26",
        "2026-04-23",
        "2026-05-21",
        "2026-06-18",
    ]


async def test_a_failing_window_raises_the_api_error_itself(fake: FakeGarmin) -> None:
    def reply(request: httpx.Request) -> Reply:
        return Reply(status=404) if "2026-01-29" in request.url.path else steps_reply(request)

    fake.respond_with("/usersummary-service/stats/steps/daily/", reply)
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        client = AsyncGarminClient(API_KEY, base_url=BASE_URL, retry=NO_WAIT, http_client=http)

        with pytest.raises(NotFoundError):
            await client.get_daily_steps("2026-01-01", "2026-03-31")


async def test_context_manager_closes_its_own_client() -> None:
    async with AsyncGarminClient(API_KEY) as client:
        http = client._http
        assert not http.is_closed
    assert http.is_closed


async def test_a_passed_http_client_stays_open() -> None:
    async with httpx.AsyncClient() as http:
        async with AsyncGarminClient(API_KEY, http_client=http):
            pass
        assert not http.is_closed


def test_max_concurrency_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        AsyncGarminClient(API_KEY, max_concurrency=0)
