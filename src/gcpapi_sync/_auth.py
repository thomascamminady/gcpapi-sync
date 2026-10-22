"""API key discovery, access tokens and token caches.

The Personal API authenticates in two steps: a long-lived *API key* (created in
Garmin Connect's settings) is exchanged for a 24-hour *access token* (a JWT), which
is sent as a bearer token on every data request. There are no refresh tokens; the
key is simply exchanged again.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from ._compat import override
from ._constants import (
    API_KEY_ENV_VAR,
    DEFAULT_API_KEY_FILE,
    DEFAULT_TOKEN_CACHE_FILE,
    FALLBACK_TOKEN_LIFETIME,
)
from .exceptions import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "AccessToken",
    "ApiKeySource",
    "FileTokenCache",
    "MemoryTokenCache",
    "TokenCache",
    "locate_api_key",
    "resolve_api_key",
    "save_api_key",
]

_LOGGER = logging.getLogger("gcpapi_sync")


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A bearer token for the data endpoints.

    Attributes:
        value: The raw token. Excluded from ``repr`` so it never ends up in logs.
        expires_at: When the token expires (timezone-aware, UTC).
    """

    value: str = field(repr=False)
    expires_at: datetime

    @classmethod
    def from_jwt(cls, value: str, *, now: datetime | None = None) -> AccessToken:
        """Build a token, reading its expiry from the JWT ``exp`` claim.

        The signature is not verified; the claim is only used to schedule refreshes.
        Tokens without a readable ``exp`` are assumed to live for 24 hours.
        """
        expiry = _jwt_expiry(value)
        if expiry is None:
            expiry = (now if now is not None else datetime.now(UTC)) + FALLBACK_TOKEN_LIFETIME
        return cls(value=value, expires_at=expiry)

    def expires_within(self, margin: timedelta, *, now: datetime | None = None) -> bool:
        """Whether the token is expired or expires within ``margin``."""
        return self.expires_at - (now if now is not None else datetime.now(UTC)) <= margin


class _JwtClaims(BaseModel):
    exp: float


class _CachedToken(BaseModel):
    """On-disk format shared with Garmin's official client."""

    fingerprint: str
    access_token: str
    expires_at: float


def _jwt_expiry(token: str) -> datetime | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = _JwtClaims.model_validate_json(payload)
        return datetime.fromtimestamp(claims.exp, tz=UTC)
    except (binascii.Error, ValidationError, ValueError, OverflowError, OSError):
        return None


def token_cache_key(base_url: str, api_key: str) -> str:
    """Cache key for tokens issued for ``api_key`` against ``base_url``.

    Matches Garmin's own client, so a shared :class:`FileTokenCache` works for both.
    """
    return hashlib.sha256(f"{base_url}\0{api_key}".encode()).hexdigest()


@runtime_checkable
class TokenCache(Protocol):
    """Storage for access tokens, keyed by a fingerprint of API key and base URL.

    Implement this to keep tokens in a keyring, a database, Redis, etc.
    Implementations used with the async client should not block for long.
    """

    def load(self, key: str) -> AccessToken | None:
        """Return the stored token for ``key``, or ``None``."""
        ...

    def save(self, key: str, token: AccessToken) -> None:
        """Store ``token`` under ``key``."""
        ...

    def clear(self) -> None:
        """Forget all stored tokens."""
        ...


class MemoryTokenCache:
    """Keeps tokens in memory for the lifetime of the process (the default)."""

    def __init__(self) -> None:
        self._tokens: dict[str, AccessToken] = {}
        self._lock = threading.Lock()

    def load(self, key: str) -> AccessToken | None:
        """Return the stored token for ``key``, or ``None``."""
        with self._lock:
            return self._tokens.get(key)

    def save(self, key: str, token: AccessToken) -> None:
        """Store ``token`` under ``key``."""
        with self._lock:
            self._tokens[key] = token

    def clear(self) -> None:
        """Forget all stored tokens."""
        with self._lock:
            self._tokens.clear()

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class FileTokenCache:
    """Persists the most recent token in a JSON file readable only by the owner.

    Reusing tokens across processes avoids one key exchange per run, which matters
    for scripts and CLIs. The default path and format are shared with Garmin's
    official client.
    """

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_TOKEN_CACHE_FILE) -> None:
        self.path = Path(path).expanduser()

    def load(self, key: str) -> AccessToken | None:
        """Return the stored token if it was issued for ``key``."""
        try:
            text = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            _LOGGER.warning("Cannot read token cache %s: %s", self.path, exc)
            return None
        _warn_if_shared(self.path, "Token cache")
        try:
            cached = _CachedToken.model_validate_json(text)
            expires_at = datetime.fromtimestamp(cached.expires_at, tz=UTC)
        except (ValidationError, OverflowError, OSError, ValueError):
            _LOGGER.warning("Ignoring malformed token cache %s", self.path)
            return None
        if cached.fingerprint != key:
            return None
        return AccessToken(value=cached.access_token, expires_at=expires_at)

    def save(self, key: str, token: AccessToken) -> None:
        """Atomically replace the cache file with ``token``."""
        cached = _CachedToken(
            fingerprint=key,
            access_token=token.value,
            expires_at=token.expires_at.timestamp(),
        )
        _write_private(self.path, cached.model_dump_json())

    def clear(self) -> None:
        """Delete the cache file."""
        self.path.unlink(missing_ok=True)

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self.path)!r})"


@dataclass(frozen=True, slots=True)
class ApiKeySource:
    """Where an API key was found.

    Attributes:
        value: The key itself. Excluded from ``repr``.
        origin: ``"argument"``, ``"environment"`` or ``"file"``.
        path: The key file, when ``origin`` is ``"file"``.
    """

    value: str = field(repr=False)
    origin: str
    path: Path | None = None

    def describe(self) -> str:
        """A short human-readable description, such as ``file ~/.config/...``."""
        if self.origin == "environment":
            return f"environment variable {API_KEY_ENV_VAR}"
        if self.path is not None:
            return f"file {self.path}"
        return self.origin


def locate_api_key(
    api_key: str | None = None,
    *,
    key_file: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> ApiKeySource:
    """Like :func:`resolve_api_key`, but also report where the key came from."""
    if api_key is not None:
        return ApiKeySource(_non_empty(api_key, "the api_key argument"), "argument")
    if key_file is not None:
        path = Path(key_file).expanduser()
        return ApiKeySource(_read_key_file(path, explicit=True), "file", path)
    environment = os.environ if env is None else env
    if (value := environment.get(API_KEY_ENV_VAR)) is not None:
        return ApiKeySource(
            _non_empty(value, f"the {API_KEY_ENV_VAR} environment variable"), "environment"
        )
    path = DEFAULT_API_KEY_FILE.expanduser()
    return ApiKeySource(_read_key_file(path, explicit=False), "file", path)


def resolve_api_key(
    api_key: str | None = None,
    *,
    key_file: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Find the Personal API key.

    Sources, in order: the ``api_key`` argument, the ``key_file`` argument, the
    ``GARMIN_PERSONAL_API_KEY`` environment variable, then
    ``~/.config/garmin/connect-personal-api-key``. An explicitly given source that is
    empty or missing is an error; it never falls through to the next one.

    Raises:
        ConfigurationError: No usable key was found.
    """
    return locate_api_key(api_key, key_file=key_file, env=env).value


def save_api_key(
    api_key: str,
    path: str | os.PathLike[str] = DEFAULT_API_KEY_FILE,
    *,
    overwrite: bool = False,
) -> Path:
    """Store an API key in a file readable only by the current user.

    The default location is where :func:`resolve_api_key` (and Garmin's own client)
    look for it.

    Raises:
        ConfigurationError: The key is empty.
        FileExistsError: The file exists and ``overwrite`` is false.
    """
    key = _non_empty(api_key, "the api_key argument")
    target = Path(path).expanduser()
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} already exists; pass overwrite=True to replace it")
    _write_private(target, key + "\n")
    return target


def _write_private(path: Path, content: str) -> None:
    """Atomically write ``content`` to ``path`` with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        Path(temporary).chmod(0o600)
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _read_key_file(path: Path, *, explicit: bool) -> str:
    try:
        value = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if explicit:
            raise ConfigurationError(f"API key file {path} does not exist") from None
        raise ConfigurationError(
            "No Garmin Personal API key found. Run `gcpapi setup`, pass api_key=..., set "
            f"{API_KEY_ENV_VAR}, or save the key to {path} (chmod 600). Create a key in "
            "Garmin Connect under Settings > Personal API."
        ) from None
    except OSError as exc:
        raise ConfigurationError(f"Cannot read API key file {path}: {exc}") from exc
    _warn_if_shared(path, "API key file")
    return _non_empty(value, f"API key file {path}")


def _non_empty(value: str, source: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ConfigurationError(f"The API key from {source} is empty")
    return stripped


def file_is_private(path: Path) -> bool:
    """Whether only the owner may access ``path`` (always true outside POSIX)."""
    if os.name != "posix":
        return True
    try:
        return not path.stat().st_mode & 0o077
    except OSError:
        return True


def _warn_if_shared(path: Path, description: str) -> None:
    if not file_is_private(path):
        _LOGGER.warning(
            "%s %s is accessible by other users; restrict it with: chmod 600 %s",
            description,
            path,
            path,
        )
