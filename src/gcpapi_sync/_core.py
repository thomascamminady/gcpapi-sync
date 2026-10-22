"""State and logic shared by the sync and async clients (no I/O happens here)."""

from __future__ import annotations

import logging
import os
import platform

import httpx

from ._auth import AccessToken, MemoryTokenCache, TokenCache, resolve_api_key, token_cache_key
from ._compat import override
from ._constants import (
    BASE_URL_ENV_VAR,
    DEFAULT_BASE_URL,
    KEY_EXCHANGE_PATH,
    TOKEN_REFRESH_MARGIN,
)
from ._retry import RetryConfig, parse_retry_after
from ._version import __version__
from .exceptions import (
    APIStatusError,
    AuthenticationError,
    BadRequestError,
    ConfigurationError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ResponseValidationError,
    decode_body,
    parse_error_body,
)

_LOGGER = logging.getLogger("gcpapi_sync")

_STATUS_ERRORS: dict[int, type[APIStatusError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
}

_HINTS: dict[int, str] = {
    400: "Garmin rejected the parameters; it sends no details for most 400s",
    401: "the access token is invalid or expired, or the API key was revoked",
    403: "this resource is not available to the authenticated user",
}


def default_user_agent() -> str:
    """The ``User-Agent`` sent with every request."""
    return (
        f"gcpapi-sync/{__version__} httpx/{httpx.__version__} "
        f"{platform.python_implementation()}/{platform.python_version()}"
    )


class BaseClient:
    """Configuration, authentication state and response handling."""

    def __init__(
        self,
        *,
        api_key: str | None,
        access_token: str | None,
        key_file: str | os.PathLike[str] | None,
        token_cache: TokenCache | None,
        base_url: str | None,
        retry: RetryConfig | None,
        user_agent: str | None,
    ) -> None:
        if base_url is None:
            base_url = os.environ.get(BASE_URL_ENV_VAR, DEFAULT_BASE_URL)
        url = base_url.rstrip("/")
        if not url.startswith(("https://", "http://")):
            raise ConfigurationError(f"base_url must start with https://, got {url!r}")
        self.base_url = url
        self.retry = retry if retry is not None else RetryConfig()
        self._user_agent = user_agent if user_agent is not None else default_user_agent()
        self._token_cache: TokenCache = (
            token_cache if token_cache is not None else MemoryTokenCache()
        )
        if access_token is not None and not access_token.strip():
            raise ConfigurationError("access_token is empty")
        self._initial_token = (
            AccessToken.from_jwt(access_token.strip()) if access_token is not None else None
        )
        # With an access token alone the client works until the token expires; it
        # can only renew it when it also has an API key.
        if access_token is not None and api_key is None and key_file is None:
            self._api_key: str | None = None
        else:
            self._api_key = resolve_api_key(api_key, key_file=key_file)
        self._cache_key = (
            token_cache_key(self.base_url, self._api_key) if self._api_key is not None else None
        )

    @property
    def can_refresh(self) -> bool:
        """Whether the client can obtain new access tokens (it has an API key)."""
        return self._api_key is not None

    @override
    def __repr__(self) -> str:
        auth = "api_key" if self._api_key is not None else "access_token"
        return f"{type(self).__name__}(base_url={self.base_url!r}, auth={auth})"

    # -- tokens ------------------------------------------------------------------------

    def _usable_token(self, stale: str | None = None) -> AccessToken | None:
        """A cached token that is neither ``stale`` (rejected by the server) nor expiring."""
        candidates = [self._initial_token]
        if self._cache_key is not None:
            candidates.insert(0, self._token_cache.load(self._cache_key))
        for token in candidates:
            if token is None or token.value == stale:
                continue
            if self._api_key is None or not token.expires_within(TOKEN_REFRESH_MARGIN):
                return token
        return None

    def _store_token(self, token: AccessToken) -> None:
        if self._cache_key is not None:
            self._token_cache.save(self._cache_key, token)
        self._initial_token = None

    def _token_request(self) -> tuple[str, dict[str, str]]:
        if self._api_key is None:
            raise ConfigurationError(
                "The access token was rejected or has expired and no API key is configured "
                "to obtain a new one. Pass api_key=... to let the client renew tokens."
            )
        headers = {
            "Authorization": f"ApiKey {self._api_key}",
            "Accept": "*/*",
            "User-Agent": self._user_agent,
        }
        return self.base_url + KEY_EXCHANGE_PATH, headers

    def _parse_token_response(self, response: httpx.Response) -> AccessToken:
        if response.status_code in {401, 403}:
            raise AuthenticationError(
                f"Garmin rejected the API key (HTTP {response.status_code}). It may be "
                "expired, deleted or mistyped; create a new one in Garmin Connect under "
                "Settings > Personal API.",
                response=response,
            )
        if response.is_error:
            raise self._status_error(response)
        value = response.text.strip().strip('"')
        if not value:
            raise ResponseValidationError(
                "The token endpoint returned an empty body", response=response
            )
        token = AccessToken.from_jwt(value)
        _LOGGER.debug("Obtained access token valid until %s", token.expires_at.isoformat())
        return token

    # -- requests ----------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return self.base_url + path

    def _headers(self, token: str, accept: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "User-Agent": self._user_agent,
        }

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float | None:
        """Seconds to wait before retrying, or ``None`` to give up.

        ``response`` is ``None`` when the attempt failed at the transport level.
        """
        if attempt >= self.retry.max_retries:
            return None
        if response is None:
            return self.retry.backoff(attempt)
        if response.status_code not in self.retry.retry_statuses:
            return None
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is None:
            return self.retry.backoff(attempt)
        return retry_after if retry_after <= self.retry.max_retry_after else None

    def _log_retry(self, attempt: int, delay: float, reason: str) -> None:
        _LOGGER.info(
            "Retrying in %.1fs (retry %d of %d): %s",
            delay,
            attempt + 1,
            self.retry.max_retries,
            reason,
        )

    @staticmethod
    def _log_response(response: httpx.Response, elapsed: float) -> None:
        _LOGGER.debug(
            "%s %s -> %d (%.0f ms)",
            response.request.method,
            response.request.url.path,
            response.status_code,
            elapsed * 1000,
        )

    @staticmethod
    def _status_error(response: httpx.Response) -> APIStatusError:
        status = response.status_code
        message = f"{response.request.method} {response.request.url.path} returned HTTP {status}"
        detail = _summarize_body(response)
        if detail:
            message += f": {detail}"
        elif status in _HINTS:
            message += f" ({_HINTS[status]})"
        if status == 429:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            return RateLimitError(message, response=response, retry_after=retry_after)
        error_type = _STATUS_ERRORS.get(status)
        if error_type is None:
            error_type = InternalServerError if status >= 500 else APIStatusError
        return error_type(message, response=response)


def _summarize_body(response: httpx.Response, limit: int = 300) -> str:
    body = decode_body(response)
    if body is None or body in ({}, []):
        return ""
    error = parse_error_body(body)
    if error is not None and (summary := error.summary()) is not None:
        return summary[:limit]
    if isinstance(body, str):
        return body[:limit]
    return response.text.strip()[:limit]
