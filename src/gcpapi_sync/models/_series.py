"""Typed access to Garmin's compact time series.

Garmin ships intraday data as rows of numbers (``[[1792533600000, 42], ...]``) plus
a separate list describing what each column means. The mixins below turn those rows
into lists of small frozen dataclasses, locating columns through the descriptors
rather than assuming their order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ._base import SeriesRow
    from ._generated import (
        BodyBatteryValueDescriptor,
        HeartRateValueDescriptor,
        MonitoringEnvironmentValueDescriptor,
        SPO2AcclimationValueDescriptor,
        SPO2ValueDescriptor,
    )

__all__ = [
    "BodyBatterySample",
    "BodyBatterySeries",
    "HeartRateSample",
    "HeartRateSeries",
    "MonitoringEnvironmentSample",
    "SpO2Sample",
    "SpO2Series",
]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class HeartRateSample:
    """One heart rate reading."""

    timestamp: datetime
    """When the reading was taken (UTC)."""
    heart_rate: int | None
    """Beats per minute; ``None`` when the device recorded no value."""


@dataclass(frozen=True, slots=True)
class BodyBatterySample:
    """One Body Battery reading."""

    timestamp: datetime
    """When the reading was taken (UTC)."""
    level: int | None
    """Body Battery level (0-100)."""


@dataclass(frozen=True, slots=True)
class SpO2Sample:
    """One pulse-ox reading."""

    timestamp: datetime
    """When the reading was taken (UTC)."""
    spo2: float | None
    """Blood oxygen saturation in percent."""
    monitoring_environment_level: float | None
    """Monitoring environment level (altitude-related), if reported."""


@dataclass(frozen=True, slots=True)
class MonitoringEnvironmentSample:
    """One monitoring-environment (altitude acclimation) reading."""

    timestamp: datetime
    """When the reading was taken (UTC)."""
    level: float | None
    """Monitoring environment level."""


@dataclass(frozen=True, slots=True)
class _Columns:
    by_key: tuple[tuple[str, int], ...]

    @classmethod
    def of(cls, descriptors: Iterable[tuple[str | None, int | None]]) -> _Columns:
        return cls(
            tuple(
                (key, index) for key, index in descriptors if key is not None and index is not None
            )
        )

    def find(
        self, *, default: int | None, exact: str | None = None, contains: str | None = None
    ) -> int | None:
        for key, index in self.by_key:
            if exact is not None and key == exact:
                return index
            if contains is not None and contains in key.lower():
                return index
        return default


def _cell(row: SeriesRow, index: int | None) -> int | float | None:
    if index is None or index >= len(row):
        return None
    return row[index]


def _timestamp(row: SeriesRow, index: int | None) -> datetime | None:
    value = _cell(row, index)
    return None if value is None else _EPOCH + timedelta(milliseconds=value)


def _int(value: float | None) -> int | None:
    return None if value is None else round(value)


def _float(value: float | None) -> float | None:
    return None if value is None else float(value)


def _heart_rate_samples(
    rows: list[SeriesRow] | None, descriptors: list[HeartRateValueDescriptor] | None
) -> list[HeartRateSample]:
    columns = _Columns.of((d.key, d.index) for d in descriptors or ())
    time_index = columns.find(exact="timestamp", default=0)
    value_index = columns.find(exact="heartrate", default=1)
    return [
        HeartRateSample(timestamp, _int(_cell(row, value_index)))
        for row in rows or ()
        if (timestamp := _timestamp(row, time_index)) is not None
    ]


class HeartRateSeries:
    """Adds :meth:`heart_rate_samples` to :class:`DailyHeartRate`."""

    if TYPE_CHECKING:
        heart_rate_values: list[SeriesRow] | None
        heart_rate_value_descriptors: list[HeartRateValueDescriptor] | None
        abnormal_hr_values_array: list[SeriesRow] | None

    def heart_rate_samples(self) -> list[HeartRateSample]:
        """The day's heart rate readings in chronological order."""
        return _heart_rate_samples(self.heart_rate_values, self.heart_rate_value_descriptors)

    def abnormal_heart_rate_samples(self) -> list[HeartRateSample]:
        """Readings flagged as abnormally high or low."""
        return _heart_rate_samples(self.abnormal_hr_values_array, self.heart_rate_value_descriptors)


class BodyBatterySeries:
    """Adds :meth:`body_battery_samples` to :class:`BodyBatteryDailyReport`."""

    if TYPE_CHECKING:
        body_battery_values_array: list[SeriesRow] | None
        body_battery_value_descriptor_list: list[BodyBatteryValueDescriptor] | None

    def body_battery_samples(self) -> list[BodyBatterySample]:
        """The day's Body Battery readings in chronological order."""
        columns = _Columns.of(
            (d.body_battery_value_descriptor_key, d.body_battery_value_descriptor_index)
            for d in self.body_battery_value_descriptor_list or ()
        )
        time_index = columns.find(exact="timestamp", default=0)
        level_index = columns.find(exact="bodyBatteryLevel", default=1)
        return [
            BodyBatterySample(timestamp, _int(_cell(row, level_index)))
            for row in self.body_battery_values_array or ()
            if (timestamp := _timestamp(row, time_index)) is not None
        ]


class SpO2Series:
    """Adds typed SpO2 accessors to :class:`DailySpO2AndAcclimation`."""

    if TYPE_CHECKING:
        spo2_hourly_averages: list[SeriesRow] | None
        spo2_hourly_averages_descriptor_list: list[SPO2AcclimationValueDescriptor] | None
        spo2_single_values: list[SeriesRow] | None
        spo2_single_values_descriptor_list: list[SPO2ValueDescriptor] | None
        monitoring_environment_values: list[SeriesRow] | None
        monitoring_environment_values_descriptor_list: (
            list[MonitoringEnvironmentValueDescriptor] | None
        )

    def spo2_hourly_samples(self) -> list[SpO2Sample]:
        """Hourly-averaged SpO2 readings in chronological order."""
        columns = _Columns.of(
            (d.spo2_value_desc_key, d.spo2_value_desc_index)
            for d in self.spo2_hourly_averages_descriptor_list or ()
        )
        return _spo2_samples(self.spo2_hourly_averages, columns)

    def spo2_single_samples(self) -> list[SpO2Sample]:
        """Individual (spot-check) SpO2 readings in chronological order."""
        columns = _Columns.of(
            (d.spo2_value_descriptor_key, d.spo2_value_descriptor_index)
            for d in self.spo2_single_values_descriptor_list or ()
        )
        return _spo2_samples(self.spo2_single_values, columns)

    def monitoring_environment_samples(self) -> list[MonitoringEnvironmentSample]:
        """Monitoring-environment (acclimation) readings in chronological order."""
        columns = _Columns.of(
            (d.mon_env_value_desc_key, d.mon_env_value_desc_index)
            for d in self.monitoring_environment_values_descriptor_list or ()
        )
        time_index = columns.find(exact="timestamp", default=0)
        level_index = columns.find(contains="monitoringenvironment", default=1)
        return [
            MonitoringEnvironmentSample(timestamp, _float(_cell(row, level_index)))
            for row in self.monitoring_environment_values or ()
            if (timestamp := _timestamp(row, time_index)) is not None
        ]


def _spo2_samples(rows: list[SeriesRow] | None, columns: _Columns) -> list[SpO2Sample]:
    time_index = columns.find(exact="timestamp", default=0)
    spo2_index = columns.find(contains="spo2", default=1)
    environment_index = columns.find(contains="monitoringenvironment", default=None)
    return [
        SpO2Sample(
            timestamp,
            _float(_cell(row, spo2_index)),
            _float(_cell(row, environment_index)),
        )
        for row in rows or ()
        if (timestamp := _timestamp(row, time_index)) is not None
    ]
