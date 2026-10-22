from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, get_type_hints

import pytest
from pydantic import JsonValue, TypeAdapter

from gcpapi_sync import models
from gcpapi_sync.models import (
    GarminDate,
    GarminModel,
    GarminTime,
    LocalDateTime,
    UtcDateTime,
)
from tests.helpers import fixture_bytes

if TYPE_CHECKING:
    from collections.abc import Mapping

MODEL_CLASSES: list[type[GarminModel]] = sorted(
    (
        value
        for value in vars(models).values()
        if inspect.isclass(value) and issubclass(value, GarminModel) and value is not GarminModel
    ),
    key=lambda cls: cls.__name__,
)

_UTC: TypeAdapter[datetime] = TypeAdapter(UtcDateTime)
_LOCAL: TypeAdapter[datetime] = TypeAdapter(LocalDateTime)
_DATE: TypeAdapter[date] = TypeAdapter(GarminDate)
_TIME: TypeAdapter[time] = TypeAdapter(GarminTime)


# -- lenient scalar types ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-21T07:15:54.0", datetime(2026, 10, 21, 7, 15, 54, tzinfo=UTC)),
        ("2026-10-21 07:15:54", datetime(2026, 10, 21, 7, 15, 54, tzinfo=UTC)),
        ("2026-10-21T09:15:54+02:00", datetime(2026, 10, 21, 7, 15, 54, tzinfo=UTC)),
        ("2026-10-21T07:15:54Z", datetime(2026, 10, 21, 7, 15, 54, tzinfo=UTC)),
        (1792559461000, datetime(2026, 10, 21, 5, 11, 1, tzinfo=UTC)),
        (1792559461123.0, datetime(2026, 10, 21, 5, 11, 1, 123000, tzinfo=UTC)),
        (
            {"date": "2026-10-21T07:15:54.0", "localDate": "2026-10-21"},
            datetime(2026, 10, 21, 7, 15, 54, tzinfo=UTC),
        ),
    ],
)
def test_utc_datetime(raw: JsonValue, expected: datetime) -> None:
    parsed = _UTC.validate_python(raw)

    assert parsed == expected
    assert parsed.tzinfo is UTC


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-21T09:15:54.0", datetime(2026, 10, 21, 9, 15, 54, tzinfo=UTC)),
        (1792566661000, datetime(2026, 10, 21, 7, 11, 1, tzinfo=UTC)),
    ],
)
def test_local_datetime_is_naive_wall_time(raw: JsonValue, expected: datetime) -> None:
    parsed = _LOCAL.validate_python(raw)

    assert parsed == expected.replace(tzinfo=None)
    assert parsed.tzinfo is None


def test_local_datetime_keeps_explicit_offsets() -> None:
    parsed = _LOCAL.validate_python("2026-10-21T09:15:54+02:00")

    assert parsed.utcoffset() == timedelta(hours=2)
    assert parsed.tzinfo == timezone(timedelta(hours=2))


@pytest.mark.parametrize(
    "raw",
    [
        "2026-10-21",
        "2026-10-21T00:00:00.0",
        "2026-10-21 13:00:00",
        1792540800000,
        {"localDate": "2026-10-21", "date": "2026-10-20T22:00:00.0"},
        {"date": "2026-10-21"},
    ],
)
def test_garmin_date(raw: JsonValue) -> None:
    assert _DATE.validate_python(raw) == date(2026, 10, 21)


def test_garmin_date_accepts_datetime_objects() -> None:
    assert _DATE.validate_python(datetime(2026, 10, 21, 23, 0, tzinfo=UTC)) == date(2026, 10, 21)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("07:00:00", time(7)),
        ("18:59", time(18, 59)),
        ({"hour": 18, "minute": 59, "second": 1, "nano": 5_000_000}, time(18, 59, 1, 5000)),
        ({"hour": 6, "minute": None, "second": True}, time(6)),
    ],
)
def test_garmin_time(raw: JsonValue, expected: time) -> None:
    assert _TIME.validate_python(raw) == expected


@pytest.mark.parametrize(
    ("adapter", "raw"), [(_UTC, True), (_DATE, False), (_UTC, "soon"), (_DATE, "today")]
)
def test_lenient_types_still_reject_garbage(
    adapter: TypeAdapter[datetime] | TypeAdapter[date], raw: JsonValue
) -> None:
    with pytest.raises(ValueError, match="validation error"):
        adapter.validate_python(raw)


# -- fixtures (scrubbed real responses) ----------------------------------------------------


FIXTURE_ADAPTERS: Mapping[str, TypeAdapter[object]] = {
    "activity": TypeAdapter(models.ActivitySummary),
    "daily_body_battery": TypeAdapter(list[models.BodyBatteryDailyReport]),
    "daily_food_logs": TypeAdapter(models.DailyNutritionSummary),
    "daily_heart_rate": TypeAdapter(models.DailyHeartRate),
    "daily_hrv": TypeAdapter(models.HrvChart),
    "daily_hrv_summaries": TypeAdapter(models.HrvDailySummary),
    "daily_sleep": TypeAdapter(models.DailySleep),
    "daily_sleep_summaries": TypeAdapter(list[models.DailySleep]),
    "daily_spo2": TypeAdapter(models.DailySpO2AndAcclimation),
    "daily_steps": TypeAdapter(list[models.StepsDailyStat]),
    "daily_stress": TypeAdapter(list[models.MultiValueWellnessChartItem]),
    "daily_summaries": TypeAdapter(models.UserDailySummaryV2List),
    "daily_summary": TypeAdapter(models.UserDailySummary),
    "effective_weight_goals": TypeAdapter(models.UserGoalsRange),
    "fitness_age": TypeAdapter(models.FitnessAge),
    "heart_rate_zones": TypeAdapter(list[models.HeartRateZones]),
    "search_activities": TypeAdapter(list[models.SummarizedActivity]),
    "user_profile_settings": TypeAdapter(models.UserSettings),
    "weight_range": TypeAdapter(models.WeightRange),
    "wellness_activity_summaries": TypeAdapter(list[models.WellnessActivitySummary]),
}


@pytest.mark.parametrize("name", sorted(FIXTURE_ADAPTERS))
def test_fixture_parses_and_round_trips(name: str) -> None:
    adapter = FIXTURE_ADAPTERS[name]
    raw = fixture_bytes(name)

    parsed = adapter.validate_json(raw)
    dumped = adapter.dump_python(parsed, by_alias=True, mode="json")

    assert (
        adapter.dump_python(adapter.validate_python(dumped), by_alias=True, mode="json") == dumped
    )
    assert not undocumented_fields(parsed), "fixture contains fields the models do not know"


def undocumented_fields(value: object) -> set[str]:
    """Names of fields that ended up in ``model_extra`` anywhere inside ``value``."""
    found: set[str] = set()
    if isinstance(value, GarminModel):
        found |= {f"{type(value).__name__}.{name}" for name in value.model_extra or {}}
        for name in type(value).model_fields:
            found |= undocumented_fields(getattr(value, name))
    elif isinstance(value, list):
        for item in value:
            found |= undocumented_fields(item)
    return found


def test_converted_fields_in_fixtures() -> None:
    activity = models.ActivitySummary.model_validate_json(fixture_bytes("activity"))
    summary = activity.summary
    assert summary is not None
    assert isinstance(summary.start_time_gmt, datetime)
    assert summary.start_time_gmt.tzinfo is UTC
    assert isinstance(summary.start_time_local, datetime)
    assert summary.start_time_local.tzinfo is None

    sleep = models.DailySleep.model_validate_json(fixture_bytes("daily_sleep"))
    assert isinstance(sleep.calendar_date, date)
    assert isinstance(sleep.sleep_start_timestamp_gmt, datetime)
    assert sleep.sleep_scores is not None
    assert isinstance(sleep.sleep_scores.overall, models.SleepScoreItem)

    stress = TypeAdapter(list[models.MultiValueWellnessChartItem]).validate_json(
        fixture_bytes("daily_stress")
    )
    assert isinstance(stress[0].values, models.StressValues)

    nutrition = models.DailyNutritionSummary.model_validate_json(fixture_bytes("daily_food_logs"))
    assert isinstance(nutrition.day_start_time, time)

    age = models.FitnessAge.model_validate_json(fixture_bytes("fitness_age"))
    assert age.components is not None
    assert isinstance(age.components.rhr, models.FitnessAgeComponent)
    assert isinstance(age.last_updated, datetime)


# -- every model ---------------------------------------------------------------------------


SCALAR_SAMPLES: dict[tuple[object, object], JsonValue] = {
    ("string", "date"): "2026-01-15",
    ("string", "date-time"): "2026-01-15T08:00:00.0",
    ("string", "time"): "08:00:00",
    ("string", "uuid"): "00000000-0000-4000-8000-000000000000",
    ("string", None): "text",
    ("integer", None): 7,
    ("number", None): 1.5,
    ("boolean", None): True,
}


def resolve(
    schema: Mapping[str, JsonValue], definitions: Mapping[str, JsonValue]
) -> Mapping[str, JsonValue]:
    """Follow ``$ref`` and pick the first non-null ``anyOf`` branch."""
    reference = schema.get("$ref")
    if isinstance(reference, str):
        target = definitions[reference.rsplit("/", 1)[1]]
        assert isinstance(target, dict)
        return resolve(target, definitions)
    options = schema.get("anyOf")
    if isinstance(options, list):
        concrete = [
            option
            for option in options
            if isinstance(option, dict) and option.get("type") != "null"
        ]
        return resolve(concrete[0], definitions) if concrete else {"type": "null"}
    return schema


def sample(schema: Mapping[str, JsonValue], definitions: Mapping[str, JsonValue]) -> JsonValue:
    """A value satisfying a pydantic JSON schema, with every optional field present."""
    schema = resolve(schema, definitions)
    kind = schema.get("type")
    items, properties = schema.get("items"), schema.get("properties")
    if kind == "array":
        return [sample(items, definitions) if isinstance(items, dict) else "value"]
    if isinstance(properties, dict):
        return {
            name: sample(child, definitions)
            for name, child in properties.items()
            if isinstance(child, dict)
        }
    if kind == "null":
        return None
    return SCALAR_SAMPLES.get((kind, schema.get("format")), "value")


@pytest.mark.parametrize("model", MODEL_CLASSES, ids=lambda cls: cls.__name__)
def test_every_field_accepts_its_documented_type(model: type[GarminModel]) -> None:
    schema = model.model_json_schema(by_alias=True)
    definitions = schema.get("$defs", {})
    assert isinstance(definitions, dict)
    payload = sample(schema, definitions)

    instance = model.model_validate(payload)

    assert all(getattr(instance, name) is not None for name in model.model_fields)
    assert instance.model_extra == {}
    assert model.model_validate(instance.model_dump(by_alias=True)) == instance


REQUIRED = {"SummarizedActivity": {"activity_id"}, "ActivitySummary": {"activity_id"}}


@pytest.mark.parametrize("model", MODEL_CLASSES, ids=lambda cls: cls.__name__)
def test_only_identifiers_are_required(model: type[GarminModel]) -> None:
    required = {name for name, field in model.model_fields.items() if field.is_required()}
    assert required == REQUIRED.get(model.__name__, set())

    instance = model.model_validate(
        {model.model_fields[name].alias or name: 1 for name in required}
    )

    assert all(
        getattr(instance, name) is None for name in model.model_fields if name not in required
    )


@pytest.mark.parametrize("model", MODEL_CLASSES, ids=lambda cls: cls.__name__)
def test_no_untyped_containers(model: type[GarminModel]) -> None:
    """Typing policy: no free-form dicts or ``Any`` in any response model."""
    for name, hint in get_type_hints(model, include_extras=True).items():
        if name.startswith("model_"):
            continue
        text = str(hint)
        assert "dict[" not in text, f"{model.__name__}.{name} is a dict: {text}"
        assert "typing.Any" not in text, f"{model.__name__}.{name} is Any: {text}"


def test_names_and_aliases() -> None:
    by_alias = models.StepsDailyStat.model_validate({"calendarDate": "2026-10-21", "totalSteps": 5})
    by_name = models.StepsDailyStat.model_validate(
        {"calendar_date": "2026-10-21", "total_steps": 5}
    )

    assert by_alias == by_name
    assert by_alias.model_dump(by_alias=True, exclude_none=True) == {
        "calendarDate": date(2026, 10, 21),
        "totalSteps": 5,
    }


def test_unknown_fields_are_kept() -> None:
    parsed = models.StepsDailyStat.model_validate({"totalSteps": 5, "brandNewField": [1, 2]})

    assert parsed.model_extra == {"brandNewField": [1, 2]}
    assert parsed.model_dump(by_alias=True)["brandNewField"] == [1, 2]


def test_every_model_is_exported() -> None:
    from gcpapi_sync.models import _generated

    generated = {
        name
        for name, value in vars(_generated).items()
        if inspect.isclass(value)
        and issubclass(value, GarminModel)
        and value.__module__ == _generated.__name__
    }

    assert generated <= set(models.__all__)
    assert set(_generated.__all__) == generated
    assert all(getattr(_generated, name).__pydantic_complete__ for name in generated)
    assert not [
        name for name in vars(_generated) if name.startswith("_") and not name.startswith("__")
    ]


# -- time series ---------------------------------------------------------------------------


def test_heart_rate_samples() -> None:
    heart_rate = models.DailyHeartRate.model_validate(
        {
            "heartRateValueDescriptors": [
                {"key": "heartrate", "index": 0},
                {"key": "timestamp", "index": 1},
            ],
            "heartRateValues": [[42, 1792533600000], [None, 1792533720000], [50, None], [61]],
            "abnormalHRValuesArray": [[150, 1792533600000]],
        }
    )

    assert heart_rate.heart_rate_samples() == [
        models.HeartRateSample(datetime(2026, 10, 20, 22, 0, tzinfo=UTC), 42),
        models.HeartRateSample(datetime(2026, 10, 20, 22, 2, tzinfo=UTC), None),
    ]
    assert heart_rate.abnormal_heart_rate_samples()[0].heart_rate == 150


def test_series_default_to_timestamp_then_value() -> None:
    heart_rate = models.DailyHeartRate.model_validate({"heartRateValues": [[1792533600000, 42.4]]})

    assert heart_rate.heart_rate_samples()[0].heart_rate == 42
    assert models.DailyHeartRate().heart_rate_samples() == []


def test_body_battery_samples_from_fixture() -> None:
    report = TypeAdapter(list[models.BodyBatteryDailyReport]).validate_json(
        fixture_bytes("daily_body_battery")
    )[0]

    samples = report.body_battery_samples()

    assert samples
    assert all(sample.timestamp.tzinfo is UTC for sample in samples)
    assert all(sample.level is None or isinstance(sample.level, int) for sample in samples)
    assert any(isinstance(sample.level, int) for sample in samples)


def test_spo2_samples() -> None:
    spo2 = models.DailySpO2AndAcclimation.model_validate(
        {
            "spO2HourlyAveragesDescriptorList": [
                {"spo2ValueDescIndex": 0, "spo2ValueDescKey": "timestamp"},
                {"spo2ValueDescIndex": 1, "spo2ValueDescKey": "spo2Level"},
                {"spo2ValueDescIndex": 2, "spo2ValueDescKey": "monitoringEnvironmentLevel"},
            ],
            "spO2HourlyAverages": [[1792533600000, 95, 61.6]],
            "spO2SingleValuesDescriptorList": [
                {"spo2ValueDescriptorIndex": 1, "spo2ValueDescriptorKey": "spo2Reading"},
                {"spo2ValueDescriptorIndex": 0, "spo2ValueDescriptorKey": "timestamp"},
            ],
            "spO2SingleValues": [[1792533600000, 97]],
            "monitoringEnvironmentValuesDescriptorList": [
                {"monEnvValueDescIndex": 0, "monEnvValueDescKey": "timestamp"},
                {"monEnvValueDescIndex": 1, "monEnvValueDescKey": "monitoringEnvironmentLevel"},
            ],
            "monitoringEnvironmentValues": [[1792533600000, 62]],
        }
    )
    when = datetime(2026, 10, 20, 22, 0, tzinfo=UTC)

    assert spo2.spo2_hourly_samples() == [models.SpO2Sample(when, 95.0, 61.6)]
    assert spo2.spo2_single_samples() == [models.SpO2Sample(when, 97.0, None)]
    assert spo2.monitoring_environment_samples() == [models.MonitoringEnvironmentSample(when, 62.0)]


def test_wrapped_values_without_content_are_rejected() -> None:
    with pytest.raises(ValueError, match="validation error"):
        _UTC.validate_python({"date": None, "localDate": None})
