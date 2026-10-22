"""Test doubles: a fake Personal API served through ``httpx.MockTransport``."""

from __future__ import annotations

import base64
import functools
import inspect
import io
import json
import zipfile
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import httpx
from anyio.from_thread import BlockingPortal, start_blocking_portal
from pydantic import JsonValue, TypeAdapter

from gcpapi_sync import AsyncGarminClient, GarminClient, RetryConfig, TokenCache

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Generator

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://garmin.test"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
API_KEY = "test-api-key"
NO_WAIT = RetryConfig(initial_delay=0, max_delay=0, jitter=0)

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def make_jwt(expires_at: datetime | None = None, *, subject: str = "test") -> str:
    """An unsigned JWT with an ``exp`` claim (defaults to 24 hours from now)."""
    expiry = expires_at if expires_at is not None else datetime.now(UTC) + timedelta(hours=24)

    def encode(part: dict[str, str | int]) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()

    claims: dict[str, str | int] = {"sub": subject, "exp": int(expiry.timestamp())}
    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


def fixture_bytes(name: str) -> bytes:
    """Raw bytes of ``tests/fixtures/<name>.json``."""
    return (FIXTURES / f"{name}.json").read_bytes()


def fixture_json(name: str) -> JsonValue:
    """Decoded ``tests/fixtures/<name>.json``."""
    return _JSON.validate_json(fixture_bytes(name))


def zip_archive(files: dict[str, bytes]) -> bytes:
    """A zip archive containing ``files``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


@dataclass(frozen=True)
class Reply:
    """A canned response (or a transport error to raise)."""

    status: int = 200
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()
    error: type[httpx.TransportError] | None = None

    @classmethod
    def json(cls, data: JsonValue, status: int = 200, **headers: str) -> Reply:
        """A JSON reply."""
        return cls(
            status=status,
            body=json.dumps(data).encode(),
            headers=(("Content-Type", "application/json"), *headers.items()),
        )

    @classmethod
    def fixture(cls, name: str) -> Reply:
        """A reply serving ``tests/fixtures/<name>.json``."""
        return cls(body=fixture_bytes(name), headers=(("Content-Type", "application/json"),))


@dataclass
class FakeGarmin:
    """A minimal stand-in for the Personal API.

    The token endpoint issues JWTs for :data:`API_KEY`; data endpoints require one of
    the issued, unrevoked tokens. Routes answer with queued replies; the last reply
    of each route repeats.
    """

    requests: list[httpx.Request] = field(default_factory=list)
    issued: list[str] = field(default_factory=list)
    revoked: set[str] = field(default_factory=set)
    token_lifetime: timedelta = timedelta(hours=24)
    token_replies: deque[Reply] = field(default_factory=deque)
    routes: dict[str, deque[Reply]] = field(default_factory=dict)
    responders: dict[str, Callable[[httpx.Request], Reply]] = field(default_factory=dict)

    def reply(self, path: str, *replies: Reply) -> None:
        """Answer requests to ``path`` with ``replies``, in order."""
        self.routes[path] = deque(replies)

    def respond_with(self, path_prefix: str, responder: Callable[[httpx.Request], Reply]) -> None:
        """Answer requests whose path starts with ``path_prefix`` by calling ``responder``."""
        self.responders[path_prefix] = responder

    def revoke_all(self) -> None:
        """Make the server reject every token issued so far."""
        self.revoked.update(self.issued)

    @property
    def data_requests(self) -> list[httpx.Request]:
        """Requests other than token exchanges."""
        return [request for request in self.requests if request.url.path != TOKEN_PATH]

    @property
    def token_requests(self) -> list[httpx.Request]:
        """Token exchanges."""
        return [request for request in self.requests if request.url.path == TOKEN_PATH]

    def transport(self) -> httpx.MockTransport:
        """A transport usable by both ``httpx.Client`` and ``httpx.AsyncClient``."""
        return httpx.MockTransport(self._handle)

    @staticmethod
    def _next(queue: deque[Reply]) -> Reply:
        return queue.popleft() if len(queue) > 1 else queue[0]

    def _respond(self, request: httpx.Request, reply: Reply) -> httpx.Response:
        if reply.error is not None:
            raise reply.error("simulated failure", request=request)
        return httpx.Response(reply.status, content=reply.body, headers=list(reply.headers))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == TOKEN_PATH:
            return self._exchange(request)
        return self._serve(request)

    def _exchange(self, request: httpx.Request) -> httpx.Response:
        if self.token_replies:
            return self._respond(request, self._next(self.token_replies))
        if request.headers.get("Authorization") != f"ApiKey {API_KEY}":
            return httpx.Response(401)
        token = make_jwt(datetime.now(UTC) + self.token_lifetime, subject=str(len(self.issued)))
        self.issued.append(token)
        return httpx.Response(200, text=token, headers={"Content-Type": "text/plain"})

    def _serve(self, request: httpx.Request) -> httpx.Response:
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if bearer not in self.issued or bearer in self.revoked:
            return httpx.Response(401)
        for prefix, responder in self.responders.items():
            if request.url.path.startswith(prefix):
                return self._respond(request, responder(request))
        queue = self.routes.get(request.url.path)
        if queue is None:
            return httpx.Response(404, json={"error": f"no route for {request.url.path}"})
        return self._respond(request, self._next(queue))


async def _collect(make: Callable[[], AsyncIterator[object]]) -> list[object]:
    return [item async for item in make()]


@dataclass
class Harness:
    """Calls client methods by name on either the sync or the async client.

    Iterators are collected into lists so tests can treat both clients alike.
    """

    kind: Literal["sync", "async"]
    fake: FakeGarmin
    sync_client: GarminClient | None = None
    async_client: AsyncGarminClient | None = None
    portal: BlockingPortal | None = None

    def call(self, method: str, /, *args: object, **kwargs: object) -> object:
        """Invoke ``method`` and return its (awaited, collected) result."""
        if self.sync_client is not None:
            result: object = getattr(self.sync_client, method)(*args, **kwargs)
            return list(result) if inspect.isgenerator(result) else result
        assert self.async_client is not None
        assert self.portal is not None
        bound = getattr(self.async_client, method)
        call = functools.partial(bound, *args, **kwargs)
        if inspect.isasyncgenfunction(bound):
            return self.portal.call(_collect, call)
        awaited: object = self.portal.call(call)
        return awaited


@contextmanager
def open_harness(
    kind: Literal["sync", "async"],
    fake: FakeGarmin,
    *,
    api_key: str | None = API_KEY,
    access_token: str | None = None,
    token_cache: TokenCache | None = None,
    retry: RetryConfig = NO_WAIT,
) -> Generator[Harness]:
    """A :class:`Harness` whose client talks to ``fake``."""
    if kind == "sync":
        with GarminClient(
            api_key,
            access_token=access_token,
            token_cache=token_cache,
            base_url=BASE_URL,
            retry=retry,
            http_client=httpx.Client(transport=fake.transport()),
        ) as client:
            yield Harness(kind, fake, sync_client=client)
        return
    with start_blocking_portal() as portal:
        http = httpx.AsyncClient(transport=fake.transport())
        async_client = AsyncGarminClient(
            api_key,
            access_token=access_token,
            token_cache=token_cache,
            base_url=BASE_URL,
            retry=retry,
            http_client=http,
        )
        try:
            yield Harness(kind, fake, async_client=async_client, portal=portal)
        finally:
            portal.call(http.aclose)
