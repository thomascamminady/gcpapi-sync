from __future__ import annotations

import hashlib
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from gcpapi_sync import (
    AccessToken,
    ConfigurationError,
    FileTokenCache,
    MemoryTokenCache,
    TokenCache,
    resolve_api_key,
)
from gcpapi_sync._auth import file_is_private, locate_api_key, save_api_key, token_cache_key
from tests.helpers import make_jwt

if TYPE_CHECKING:
    from pathlib import Path

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions")
DEFAULT_KEY = ".config/garmin/connect-personal-api-key"


# -- access tokens -------------------------------------------------------------------------


def test_access_token_reads_the_jwt_expiry() -> None:
    expiry = datetime(2026, 10, 23, 6, 0, tzinfo=UTC)

    token = AccessToken.from_jwt(make_jwt(expiry))

    assert token.expires_at == expiry
    assert "eyJ" not in repr(token)


@pytest.mark.parametrize("value", ["opaque-token", "a.b.c", "a.bm90LWpzb24.c", "a.e30.c"])
def test_access_token_without_readable_expiry_lives_24_hours(value: str) -> None:
    now = datetime(2026, 10, 22, tzinfo=UTC)

    token = AccessToken.from_jwt(value, now=now)

    assert token.expires_at == now + timedelta(hours=24)


def test_expires_within() -> None:
    now = datetime(2026, 10, 22, 12, tzinfo=UTC)
    token = AccessToken("t", now + timedelta(minutes=10))

    assert not token.expires_within(timedelta(minutes=5), now=now)
    assert token.expires_within(timedelta(minutes=10), now=now)
    assert AccessToken("t", datetime.now(UTC) - timedelta(seconds=1)).expires_within(timedelta(0))


def test_token_cache_key_matches_garmins_client() -> None:
    expected = hashlib.sha256(b"https://host\0key").hexdigest()

    assert token_cache_key("https://host", "key") == expected


# -- key discovery -------------------------------------------------------------------------


def write_default_key(home: Path, value: str = "file-key\n") -> Path:
    path = home / DEFAULT_KEY
    path.parent.mkdir(parents=True)
    path.write_text(value)
    path.chmod(0o600)
    return path


def test_explicit_key_wins(isolated_environment: Path, tmp_path: Path) -> None:
    write_default_key(isolated_environment)
    key_file = tmp_path / "key"
    key_file.write_text("explicit-file")

    assert (
        resolve_api_key("  explicit \n", key_file=key_file, env={"GARMIN_PERSONAL_API_KEY": "env"})
        == "explicit"
    )
    assert (
        resolve_api_key(key_file=key_file, env={"GARMIN_PERSONAL_API_KEY": "env"})
        == "explicit-file"
    )
    assert resolve_api_key(env={"GARMIN_PERSONAL_API_KEY": "env"}) == "env"
    assert resolve_api_key(env={}) == "file-key"


def test_environment_variable_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", "from-env")

    source = locate_api_key()

    assert source.value == "from-env"
    assert source.origin == "environment"
    assert source.describe() == "environment variable GARMIN_PERSONAL_API_KEY"
    assert "from-env" not in repr(source)


def test_locate_describes_files_and_arguments(isolated_environment: Path) -> None:
    path = write_default_key(isolated_environment)

    assert locate_api_key().describe() == f"file {path}"
    assert locate_api_key("k").describe() == "argument"


def test_missing_key_explains_how_to_configure_one() -> None:
    with pytest.raises(ConfigurationError, match="gcpapi setup"):
        resolve_api_key(env={})


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_empty_keys_are_rejected(value: str, tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text(value)
    with pytest.raises(ConfigurationError, match="empty"):
        resolve_api_key(value)
    with pytest.raises(ConfigurationError, match="empty"):
        resolve_api_key(key_file=key_file)
    with pytest.raises(ConfigurationError, match="empty"):
        resolve_api_key(env={"GARMIN_PERSONAL_API_KEY": value})


def test_explicit_missing_key_file_does_not_fall_back(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        resolve_api_key(key_file=tmp_path / "missing", env={"GARMIN_PERSONAL_API_KEY": "env"})


def test_unreadable_key_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Cannot read"):
        resolve_api_key(key_file=tmp_path)


@posix_only
def test_shared_key_file_warns(
    isolated_environment: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = write_default_key(isolated_environment)
    path.chmod(0o644)

    with caplog.at_level(logging.WARNING, logger="gcpapi_sync"):
        resolve_api_key(env={})

    assert "chmod 600" in caplog.text
    assert not file_is_private(path)


def test_file_is_private_tolerates_missing_files(tmp_path: Path) -> None:
    assert file_is_private(tmp_path / "missing")


# -- saving keys ---------------------------------------------------------------------------


def test_save_api_key(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "key"

    assert save_api_key(" secret \n", target) == target
    assert target.read_text() == "secret\n"
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        save_api_key("other", target)
    save_api_key("other", target, overwrite=True)
    assert resolve_api_key(key_file=target) == "other"
    with pytest.raises(ConfigurationError):
        save_api_key("  ", tmp_path / "blank")


def test_save_api_key_cleans_up_after_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.replace", fail)

    with pytest.raises(OSError, match="disk full"):
        save_api_key("secret", tmp_path / "out" / "key")
    assert list((tmp_path / "out").iterdir()) == []


# -- token caches --------------------------------------------------------------------------


def test_caches_implement_the_protocol(tmp_path: Path) -> None:
    assert isinstance(MemoryTokenCache(), TokenCache)
    assert isinstance(FileTokenCache(tmp_path / "t"), TokenCache)


def test_memory_cache() -> None:
    cache = MemoryTokenCache()
    token = AccessToken("value", datetime(2030, 1, 1, tzinfo=UTC))

    assert cache.load("k") is None
    cache.save("k", token)
    assert cache.load("k") == token
    cache.clear()
    assert cache.load("k") is None
    assert repr(cache) == "MemoryTokenCache()"


def test_file_cache_round_trip(tmp_path: Path) -> None:
    cache = FileTokenCache(tmp_path / "dir" / "token.json")
    token = AccessToken("value", datetime(2030, 1, 1, 12, 30, tzinfo=UTC))

    assert cache.load("k") is None
    cache.save("k", token)

    assert cache.load("k") == token
    assert cache.load("other key") is None
    assert FileTokenCache(cache.path).load("k") == token
    if os.name == "posix":
        assert cache.path.stat().st_mode & 0o777 == 0o600
    assert repr(cache) == f"FileTokenCache({str(cache.path)!r})"
    cache.clear()
    assert not cache.path.exists()
    cache.clear()


def test_file_cache_reads_garmins_format(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    path.write_text('{"fingerprint": "k", "access_token": "abc", "expires_at": 1893456000.0}')

    token = FileTokenCache(path).load("k")

    assert token == AccessToken("abc", datetime(2030, 1, 1, tzinfo=UTC))


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "{}",
        '{"fingerprint": "k"}',
        '{"fingerprint": "k", "access_token": "a", "expires_at": 1e300}',
    ],
)
def test_file_cache_ignores_malformed_files(
    tmp_path: Path, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "token.json"
    path.write_text(content)

    with caplog.at_level(logging.WARNING, logger="gcpapi_sync"):
        assert FileTokenCache(path).load("k") is None

    assert "malformed" in caplog.text


def test_file_cache_ignores_unreadable_paths(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="gcpapi_sync"):
        assert FileTokenCache(tmp_path).load("k") is None

    assert "Cannot read token cache" in caplog.text
