"""Client construction and configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from gcpapi_sync import AsyncGarminClient, ConfigurationError, GarminClient, __version__
from tests.helpers import API_KEY, BASE_URL, FakeGarmin, Reply, make_jwt

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("client_type", [GarminClient, AsyncGarminClient])
def test_missing_credentials(client_type: type[GarminClient | AsyncGarminClient]) -> None:
    with pytest.raises(ConfigurationError, match="No Garmin Personal API key"):
        client_type()


def test_key_from_environment_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", "env-key")
    assert GarminClient().can_refresh
    key_file = tmp_path / "key"
    key_file.write_text("file-key")
    assert GarminClient(key_file=key_file).can_refresh


def test_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    assert GarminClient(API_KEY).base_url == "https://personal.connectapi.garmin.com"
    assert (
        GarminClient(API_KEY, base_url="https://example.test/").base_url == "https://example.test"
    )
    monkeypatch.setenv("GARMIN_PERSONAL_API_BASE_URL", "http://localhost:8080")
    assert GarminClient(API_KEY).base_url == "http://localhost:8080"
    with pytest.raises(ConfigurationError, match="https://"):
        GarminClient(API_KEY, base_url="ftp://example.test")


def test_repr_never_shows_secrets() -> None:
    assert repr(GarminClient("super-secret")) == (
        "GarminClient(base_url='https://personal.connectapi.garmin.com', auth=api_key)"
    )
    token = make_jwt()
    assert token not in repr(AsyncGarminClient(access_token=token))
    assert "auth=access_token" in repr(AsyncGarminClient(access_token=token))


def test_empty_access_token_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="empty"):
        GarminClient(access_token="  ")


def test_access_token_only_client_cannot_refresh() -> None:
    assert not GarminClient(access_token=make_jwt()).can_refresh


def test_user_agent(fake: FakeGarmin) -> None:
    fake.reply("/x", Reply.json({}))
    with GarminClient(
        API_KEY,
        base_url=BASE_URL,
        user_agent="my-app/1.0",
        http_client=httpx.Client(transport=fake.transport()),
    ) as client:
        client.get_json("/x")
    assert {request.headers["User-Agent"] for request in fake.requests} == {"my-app/1.0"}
    assert __version__ in GarminClient(API_KEY)._user_agent


def test_context_manager_closes_its_own_client() -> None:
    with GarminClient(API_KEY) as client:
        http = client._http
        assert not http.is_closed
    assert http.is_closed


def test_a_passed_http_client_stays_open() -> None:
    with httpx.Client() as http:
        with GarminClient(API_KEY, http_client=http):
            pass
        assert not http.is_closed


def test_timeout_is_applied() -> None:
    with GarminClient(API_KEY, timeout=3.5) as client:
        assert client._http.timeout == httpx.Timeout(3.5)
