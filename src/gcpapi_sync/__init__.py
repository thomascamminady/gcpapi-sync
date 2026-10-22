"""Typed sync and async client for the Garmin Connect Personal API.

Start with `GarminClient` (or `AsyncGarminClient`); response models are in
`gcpapi_sync.models`, exceptions in `gcpapi_sync.exceptions`.

.. include:: ../../README.md
"""

from . import models
from ._async_client import AsyncGarminClient
from ._auth import AccessToken, FileTokenCache, MemoryTokenCache, TokenCache, resolve_api_key
from ._client import GarminClient
from ._dates import DateLike
from ._endpoints import ActivityFilters, BoundingBox
from ._files import ActivityFile
from ._retry import RetryConfig
from ._version import __version__
from .exceptions import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConfigurationError,
    GarminError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ResponseValidationError,
)

__all__ = [
    "APIConnectionError",
    "APIError",
    "APIStatusError",
    "APITimeoutError",
    "AccessToken",
    "ActivityFile",
    "ActivityFilters",
    "AsyncGarminClient",
    "AuthenticationError",
    "BadRequestError",
    "BoundingBox",
    "ConfigurationError",
    "DateLike",
    "FileTokenCache",
    "GarminClient",
    "GarminError",
    "InternalServerError",
    "MemoryTokenCache",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "ResponseValidationError",
    "RetryConfig",
    "TokenCache",
    "__version__",
    "models",
    "resolve_api_key",
]
