"""Base model and lenient scalar types shared by all response models.

Garmin's API encodes the same concept in several ways, sometimes contradicting its
own OpenAPI document: timestamps arrive as ISO strings without an offset
(``"2026-10-21T07:15:54.0"``) or as epoch milliseconds, and dates as plain strings
or ``{"date": ..., "localDate": ...}`` objects. The annotated types below accept
every observed form and normalize it to one Python type.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict

__all__ = [
    "GarminDate",
    "GarminModel",
    "GarminTime",
    "LocalDateTime",
    "SeriesRow",
    "UtcDateTime",
]

# Naive on purpose, see _from_epoch_ms.
_EPOCH = datetime.fromtimestamp(0, tz=UTC).replace(tzinfo=None)


class GarminModel(BaseModel):
    """Base class for every Garmin response model.

    * Field names are ``snake_case``; Garmin's ``camelCase`` names are the aliases,
      so ``model_validate`` accepts either and ``model_dump(by_alias=True)``
      reproduces Garmin's JSON.
    * Every field is optional. Garmin omits or nulls fields freely.
    * Unknown fields are kept (see ``model_extra``), so new API fields never break
      parsing and are not silently dropped.
    """

    model_config = ConfigDict(
        extra="allow",
        validate_by_alias=True,
        validate_by_name=True,
        use_attribute_docstrings=True,
        protected_namespaces=(),
    )


def _from_epoch_ms(value: float) -> datetime:
    # Garmin's "local" epoch values are wall-clock times shifted into UTC, so the
    # same arithmetic yields UTC wall time for GMT fields and local wall time for
    # local fields. timedelta keeps millisecond precision exact.
    return _EPOCH + timedelta(milliseconds=value)


def _unwrap(value: object, *keys: str) -> object:
    if isinstance(value, dict):
        for key in keys:
            if value.get(key) is not None:
                return value[key]
    return value


def _coerce_datetime(value: object) -> object:
    value = _unwrap(value, "date", "localDate")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return _from_epoch_ms(value)
    return value


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _coerce_date(value: object) -> object:
    value = _unwrap(value, "localDate", "date")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return _from_epoch_ms(value).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and len(value) > 10 and value[10] in "T ":
        return value[:10]
    return value


def _time_part(parts: dict[object, object], key: str) -> int:
    value = parts.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _coerce_time(value: object) -> object:
    if isinstance(value, dict):
        return time(
            hour=_time_part(value, "hour"),
            minute=_time_part(value, "minute"),
            second=_time_part(value, "second"),
            microsecond=_time_part(value, "nano") // 1000,
        )
    return value


UtcDateTime = Annotated[datetime, BeforeValidator(_coerce_datetime), AfterValidator(_as_utc)]
"""A timezone-aware UTC ``datetime``.

Used for fields Garmin reports in GMT/UTC. Accepts ISO strings with or without an
offset and epoch milliseconds.
"""

LocalDateTime = Annotated[datetime, BeforeValidator(_coerce_datetime)]
"""A naive ``datetime`` holding the wall-clock time in the user's (or activity's) zone.

Garmin does not say which zone that is, so no ``tzinfo`` is attached. Accepts ISO
strings and epoch milliseconds.
"""

GarminDate = Annotated[date, BeforeValidator(_coerce_date)]
"""A calendar ``date``. Accepts ``YYYY-MM-DD``, ISO date-times, epoch ms and date objects."""

GarminTime = Annotated[time, BeforeValidator(_coerce_time)]
"""A time of day. Accepts ``HH:MM[:SS]`` strings and ``{hour, minute, second, nano}`` objects."""

SeriesRow = list[int | float | None]
"""One row of a time series.

Columns are described by the sibling ``*_descriptors`` field; the first column is
usually an epoch-millisecond timestamp.
"""
