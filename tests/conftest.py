from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

import httpx
import pytest

from gcpapi_sync import GarminClient
from tests.helpers import API_KEY, BASE_URL, NO_WAIT, FakeGarmin, Harness, open_harness

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def isolated_environment(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Keep tests away from the real key, token cache and environment (except live tests)."""
    if request.node.get_closest_marker("live") is not None:
        return Path.home()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("GARMIN_PERSONAL_API_KEY", raising=False)
    monkeypatch.delenv("GARMIN_PERSONAL_API_BASE_URL", raising=False)
    return home


@pytest.fixture
def fake() -> FakeGarmin:
    return FakeGarmin()


@pytest.fixture(params=["sync", "async"])
def harness(request: pytest.FixtureRequest, fake: FakeGarmin) -> Iterator[Harness]:
    kind: Literal["sync", "async"] = "sync" if request.param == "sync" else "async"
    with open_harness(kind, fake) as opened:
        yield opened


@pytest.fixture
def client(fake: FakeGarmin) -> Iterator[GarminClient]:
    with GarminClient(
        API_KEY,
        base_url=BASE_URL,
        retry=NO_WAIT,
        http_client=httpx.Client(transport=fake.transport()),
    ) as garmin:
        yield garmin
