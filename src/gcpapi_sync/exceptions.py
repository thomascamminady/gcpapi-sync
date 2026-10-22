"""Exceptions raised by the client.

Everything derives from :class:`GarminError`::

    GarminError
    ├── ConfigurationError            no API key found, conflicting options
    └── APIError                      anything that went wrong talking to Garmin
        ├── APIConnectionError        network failure (after retries)
        │   └── APITimeoutError
        ├── APIStatusError            non-2xx response (after retries)
        │   ├── BadRequestError       400
        │   ├── AuthenticationError   401 (key or token invalid/expired)
        │   ├── PermissionDeniedError 403
        │   ├── NotFoundError         404
        │   ├── RateLimitError        429
        │   └── InternalServerError   5xx
        └── ResponseValidationError   2xx response that does not match the models
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter
from pydantic import ValidationError as _PydanticValidationError

if TYPE_CHECKING:
    import httpx
    from pydantic import ValidationError

__all__ = [
    "APIConnectionError",
    "APIError",
    "APIStatusError",
    "APITimeoutError",
    "AuthenticationError",
    "BadRequestError",
    "ConfigurationError",
    "ErrorBody",
    "GarminError",
    "InternalServerError",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "ResponseValidationError",
]


class GarminError(Exception):
    """Base class for every exception raised by this package."""


class ConfigurationError(GarminError):
    """The client is misconfigured, for example no API key could be found."""


class APIError(GarminError):
    """A request to the Garmin API failed."""

    def __init__(self, message: str, *, request: httpx.Request | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.request = request


class APIConnectionError(APIError):
    """The API could not be reached (DNS, TLS, connection reset, ...)."""


class APITimeoutError(APIConnectionError):
    """The request timed out."""


class ErrorBody(BaseModel):
    """The JSON error document Garmin returns for some failures.

    Most errors carry an empty body (``{}``); then every field is ``None``.
    """

    model_config = ConfigDict(extra="allow", validate_by_alias=True, validate_by_name=True)

    error: str | None = None
    """Error class name, such as ``NotAcceptableException``."""
    client_message: str | None = Field(default=None, alias="clientMessage")
    """Human-readable message."""
    message: str | None = None
    """Alternative message field used by some services."""
    error_id: str | None = Field(default=None, alias="errorId")
    """Reference to quote when contacting Garmin."""

    def summary(self) -> str | None:
        """A one-line description, or ``None`` if the body says nothing useful."""
        text = self.client_message or self.message or self.error
        if text is None:
            return None
        return f"{text} (errorId {self.error_id})" if self.error_id is not None else text


class APIStatusError(APIError):
    """Garmin answered with a non-success HTTP status.

    Attributes:
        status_code: The HTTP status code.
        response: The raw :class:`httpx.Response`.
        body: The decoded JSON body, the raw text if it is not JSON, or ``None``.
        error: The body parsed as Garmin's error document, if it is one.
    """

    def __init__(self, message: str, *, response: httpx.Response) -> None:
        super().__init__(message, request=response.request)
        self.response = response
        self.status_code = response.status_code
        self.body: JsonValue = decode_body(response)
        self.error: ErrorBody | None = parse_error_body(self.body)

    @property
    def error_id(self) -> str | None:
        """Garmin's error reference (``errorId``), if the body contains one."""
        return self.error.error_id if self.error is not None else None


class BadRequestError(APIStatusError):
    """HTTP 400: malformed or unsupported parameters."""


class AuthenticationError(APIStatusError):
    """HTTP 401: the API key or access token is invalid, expired or revoked."""


class PermissionDeniedError(APIStatusError):
    """HTTP 403: the authenticated user may not access this resource."""


class NotFoundError(APIStatusError):
    """HTTP 404: the resource does not exist."""


class RateLimitError(APIStatusError):
    """HTTP 429: too many requests.

    Attributes:
        retry_after: Seconds the server asked to wait, if it said so.
    """

    def __init__(
        self, message: str, *, response: httpx.Response, retry_after: float | None
    ) -> None:
        super().__init__(message, response=response)
        self.retry_after = retry_after


class InternalServerError(APIStatusError):
    """HTTP 5xx: Garmin failed to handle the request."""


class ResponseValidationError(APIError):
    """A successful response did not match the expected model.

    The undecoded payload is kept so callers can still get at the data.

    Attributes:
        response: The raw :class:`httpx.Response`.
        validation_error: The underlying :class:`pydantic.ValidationError`, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        response: httpx.Response,
        validation_error: ValidationError | None = None,
    ) -> None:
        super().__init__(message, request=response.request)
        self.response = response
        self.validation_error = validation_error

    @property
    def raw(self) -> bytes:
        """The response body."""
        return self.response.content


_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def decode_body(response: httpx.Response) -> JsonValue:
    """Decode a response body as JSON, falling back to its text (``None`` if empty)."""
    if not response.content.strip():
        return None
    try:
        return _JSON.validate_json(response.content)
    except _PydanticValidationError:
        return response.text


def parse_error_body(body: JsonValue) -> ErrorBody | None:
    """Interpret a decoded body as an :class:`ErrorBody`, if it is a JSON object."""
    if not isinstance(body, dict):
        return None
    try:
        return ErrorBody.model_validate(body)
    except _PydanticValidationError:
        return None
