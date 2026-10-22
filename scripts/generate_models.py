# /// script
# requires-python = ">=3.11"
# dependencies = ["fire>=0.7", "ruff>=0.11"]
# ///
"""Generate ``src/gcpapi_sync/models/_generated.py`` from Garmin's OpenAPI document.

Garmin publishes the Personal API spec only inside the JavaScript bundle of its
Swagger page, so this script locates that bundle, extracts the embedded JSON, applies
corrections for places where the live API contradicts the spec, and writes pydantic
models.

Usage::

    uv run scripts/generate_models.py                    # fetch the current spec
    uv run scripts/generate_models.py --spec openapi.json  # use a local copy
    uv run scripts/generate_models.py --dump-spec openapi.json  # save the spec
"""

from __future__ import annotations

import copy
import json
import keyword
import re
import subprocess
import sys
import textwrap
import urllib.request
from pathlib import Path
from typing import Any

import fire

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "src" / "gcpapi_sync" / "models"
SWAGGER_PAGE = "https://developer.garmin.com/gc-developer-program/personal/swagger-docs/"
BUNDLE_RE = re.compile(
    r'src="(/component---src-pages-gc-developer-program-personal-swagger-docs-js-[^"]+\.js)"'
)

Schema = dict[str, Any]

# --------------------------------------------------------------------------------------
# Corrections. Each one was verified against live API responses (October 2026).
# --------------------------------------------------------------------------------------

# Fields the live API returns that the spec does not document.
EXTRA_FIELDS: dict[str, dict[str, Schema]] = {
    "BodyBatteryActivityEventDTO": {
        "activityId": {
            "type": "integer",
            "format": "int64",
            "description": "ID of the activity behind this event, if any.",
        },
        "activityName": {
            "type": "string",
            "description": "Name of the activity behind this event, if any.",
        },
        "activityType": {
            "type": "string",
            "description": "Activity type key, such as ``running``.",
        },
        "deviceId": {
            "type": "integer",
            "format": "int64",
            "description": "ID of the device that recorded the event.",
        },
        "eventUpdateTimeGmt": {
            "type": "string",
            "format": "date-time",
            "description": "When the event was last updated, in UTC.",
        },
    },
    "DailySleepDTO": {
        "retro": {
            "type": "boolean",
            "description": "Whether the sleep record was entered retroactively.",
        },
    },
    "DateWeightDTO": {
        "samplePk": {
            "type": "integer",
            "format": "int64",
            "description": "Primary key of the weight sample.",
        },
        "sourceType": {
            "type": "string",
            "description": "Source of the measurement, such as ``INDEX_SCALE`` or ``MANUAL``.",
        },
    },
    "GarminUUID": {
        "uuid": {
            "type": "string",
            "format": "uuid",
            "description": "The UUID in canonical string form.",
        },
    },
    "HydrationV2DTO": {
        "consumedInMl": {
            "type": "number",
            "format": "double",
            "description": "Hydration consumed, in milliliters.",
        },
    },
    "UserInfoDto": {
        "userPro": {"type": "boolean", "description": "Whether the user has a pro account."},
    },
}


def _score_item(description: str) -> Schema:
    return {"$ref": "#/components/schemas/SleepScoreItem", "description": description}


def _fitness_component(description: str) -> Schema:
    return {"$ref": "#/components/schemas/FitnessAgeComponent", "description": description}


def _seconds(description: str) -> Schema:
    return {"type": "number", "format": "double", "description": description}


# Free-form maps in the spec whose keys are in fact fixed. They become models so that
# every value is reachable as a typed attribute (unknown keys still land in
# ``model_extra``). Keys verified against live responses.
NEW_SCHEMAS: dict[str, Schema] = {
    "StressValues": {
        "type": "object",
        "description": "Stress metrics for one day. Durations are in seconds.",
        "properties": {
            "overallStressLevel": {
                "type": "number",
                "format": "double",
                "description": "Average stress level (0-100); negative when not enough data.",
            },
            "restStressDuration": _seconds("Time at rest (stress 0-25), in seconds."),
            "lowStressDuration": _seconds("Time with low stress (26-50), in seconds."),
            "mediumStressDuration": _seconds("Time with medium stress (51-75), in seconds."),
            "highStressDuration": _seconds("Time with high stress (76-100), in seconds."),
        },
    },
    "FitnessAgeComponents": {
        "type": "object",
        "description": "The inputs that determine Fitness Age.",
        "properties": {
            "vigorousDaysAvg": _fitness_component("Average vigorous-activity days per week."),
            "vigorousMinutesAvg": _fitness_component(
                "Average vigorous intensity minutes per week."
            ),
            "rhr": _fitness_component("Resting heart rate."),
            "bodyFat": _fitness_component("Body fat percentage."),
            "bmi": _fitness_component("Body mass index (used when body fat is unavailable)."),
        },
    },
    "SleepScores": {
        "type": "object",
        "description": "The overall sleep score and its contributing factors.",
        "properties": {
            "overall": _score_item("Overall sleep score."),
            "totalDuration": _score_item("Total sleep duration."),
            "stress": _score_item("Average stress during sleep."),
            "awakeCount": _score_item("Number of awakenings."),
            "restlessness": _score_item("Restlessness."),
            "remPercentage": _score_item("Share of REM sleep."),
            "lightPercentage": _score_item("Share of light sleep."),
            "deepPercentage": _score_item("Share of deep sleep."),
        },
    },
}

# The only required fields: identifiers without which a record is meaningless and
# which every live response contains. Everything else is optional, since Garmin omits
# and nulls fields freely. Requiring these lets callers pass ``activity.activity_id``
# straight to ``get_activity`` and friends without a ``None`` check.
REQUIRED_FIELDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("SummarizedActivityDTO", "activityId"),
        ("ActivitySummaryDTO", "activityId"),
    }
)

# Property schema replacements: (schema, field) -> new property schema.
FIELD_SCHEMAS: dict[tuple[str, str], Schema] = {
    ("MultiValueWellnessChartItemDTO", "values"): {"$ref": "#/components/schemas/StressValues"},
    ("FitnessAgeDTO", "components"): {"$ref": "#/components/schemas/FitnessAgeComponents"},
    ("DailySleepDTO", "sleepScores"): {"$ref": "#/components/schemas/SleepScores"},
    ("FitnessAgeDTO", "lastUpdated"): {"type": "string", "format": "date-time"},
}

# Hand-written mixins (from ``_series.py``) adding typed accessors to time series.
MIXINS: dict[str, str] = {
    "DailyHeartRateDTO": "HeartRateSeries",
    "BodyBatteryDailyReportDTO": "BodyBatterySeries",
    "DailySpO2AndAcclimationDTO": "SpO2Series",
}

# Schemas that the API serializes as scalars, not as the objects the spec describes
# (see ``_ref_annotation`` for the types used instead).
SCALAR_SCHEMAS = frozenset({"ConnectDate", "ConnectTimestamp", "LocalTime"})

# Class-name overrides for schemas whose stripped names would be awkward or clash.
CLASS_NAMES: dict[str, str] = {
    "UUID": "UUIDValue",
    "ApiDocumentListUserDailySummaryV2DTO": "UserDailySummaryV2List",
}

# Field-name overrides: (schema, field) -> python attribute name.
FIELD_NAMES: dict[tuple[str, str], str] = {}

_UTC_DESCRIPTION = re.compile(
    r"\bin UTC\b|UTC (time|timestamp|date)|UTC timezone|RFC 3339|Unix epoch", re.IGNORECASE
)
_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}\S{0,3}-\S{0,3}\d{2}\S{0,3}-\S{0,3}\d{2}\S{0,3}T|\\d\{4\}-\\d\{2\}-\\d\{2\}T"
)
_DATETIME_DESCRIPTION = re.compile(r"yyyy-MM-dd[ 'T]+HH:mm", re.IGNORECASE)
_EPOCH_MS_DESCRIPTION = re.compile(r"milliseconds since the Unix epoch", re.IGNORECASE)
_SPECIAL_TOKENS = [
    ("SpO2", "Spo2"),
    ("spO2", "spo2"),
    ("SPO2", "Spo2"),
    ("vO2", "vo2"),
    ("VO2", "Vo2"),
]


# --------------------------------------------------------------------------------------
# Spec loading
# --------------------------------------------------------------------------------------


def _http_get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "gcpapi-sync model generator"})
    with urllib.request.urlopen(request, timeout=60) as response:
        body: bytes = response.read()
    return body.decode("utf-8")


def _decode_js_string(source: str, start: int) -> str:
    """Decode a single-quoted JavaScript string literal starting at ``start``."""
    simple = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
    out: list[str] = []
    i = start
    while True:
        char = source[i]
        if char == "\\":
            nxt = source[i + 1]
            if nxt == "u":
                out.append(chr(int(source[i + 2 : i + 6], 16)))
                i += 6
            elif nxt == "x":
                out.append(chr(int(source[i + 2 : i + 4], 16)))
                i += 4
            else:
                out.append(simple.get(nxt, nxt))
                i += 2
            continue
        if char == "'":
            return "".join(out)
        out.append(char)
        i += 1


def fetch_spec() -> Schema:
    """Download the OpenAPI document embedded in Garmin's Swagger page."""
    page = _http_get(SWAGGER_PAGE)
    match = BUNDLE_RE.search(page)
    if match is None:
        raise SystemExit(
            "Could not find the Swagger bundle on Garmin's page; the site layout changed."
        )
    bundle = _http_get("https://developer.garmin.com" + match.group(1))
    marker = 'JSON.parse(\'{"openapi"'
    index = bundle.find(marker)
    if index < 0:
        raise SystemExit("Could not find the embedded OpenAPI document in the Swagger bundle.")
    spec: Schema = json.loads(_decode_js_string(bundle, index + len("JSON.parse('")), strict=False)
    return spec


def _warn(message: str) -> None:
    # Printed, not raised: the weekly drift job turns these into a pull request note.
    print(f"warning: {message}")


def _add_new_schemas(schemas: dict[str, Schema]) -> None:
    for name, schema in NEW_SCHEMAS.items():
        if name in schemas:
            _warn(f"the spec now defines {name}; review NEW_SCHEMAS")
        schemas.setdefault(name, schema)


def _add_extra_fields(schemas: dict[str, Schema]) -> None:
    for schema_name, fields in EXTRA_FIELDS.items():
        if schema_name not in schemas:
            _warn(f"{schema_name} is gone from the spec; drop it from EXTRA_FIELDS")
            continue
        properties = schemas[schema_name].setdefault("properties", {})
        for field_name, field_schema in fields.items():
            if field_name in properties:
                _warn(f"{schema_name}.{field_name} is now in the spec; drop it from EXTRA_FIELDS")
            properties.setdefault(field_name, field_schema)


def _replace_fields(schemas: dict[str, Schema]) -> None:
    for (schema_name, field_name), replacement in FIELD_SCHEMAS.items():
        properties = schemas.get(schema_name, {}).get("properties", {})
        if field_name not in properties:
            _warn(f"{schema_name}.{field_name} is gone from the spec; drop it from FIELD_SCHEMAS")
            continue
        original = properties[field_name]
        properties[field_name] = {"description": original.get("description", ""), **replacement}


def _check_references(schemas: dict[str, Schema]) -> None:
    for schema_name, field_name in REQUIRED_FIELDS:
        if field_name not in schemas.get(schema_name, {}).get("properties", {}):
            _warn(f"{schema_name}.{field_name} is gone from the spec; drop it from REQUIRED_FIELDS")
    for schema_name in MIXINS:
        if schema_name not in schemas:
            _warn(f"{schema_name} is gone from the spec; drop it from MIXINS")


def patch_spec(spec: Schema) -> Schema:
    """Apply the corrections above to a copy of the spec."""
    spec = copy.deepcopy(spec)
    schemas: dict[str, Schema] = spec["components"]["schemas"]
    _add_new_schemas(schemas)
    _add_extra_fields(schemas)
    _replace_fields(schemas)
    _check_references(schemas)
    return spec


def endpoint_surface(spec: Schema) -> Schema:
    """A compact, diff-friendly summary of every endpoint (no descriptions)."""
    endpoints: list[Schema] = []
    for path, operations in sorted(spec["paths"].items()):
        for method, operation in sorted(operations.items()):
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            parameters = [
                {
                    "name": parameter["name"],
                    "in": parameter["in"],
                    "required": bool(parameter.get("required", False)),
                    "type": parameter.get("schema", {}).get("type", "string"),
                }
                for parameter in operation.get("parameters", [])
            ]
            responses = operation.get("responses", {})
            success = responses.get("200") or responses.get("default") or {}
            content: Schema = next(iter(success.get("content", {}).values()), {})
            endpoints.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "parameters": sorted(parameters, key=lambda p: (p["in"], p["name"])),
                    "response": _schema_label(content.get("schema")),
                }
            )
    return {
        "source": SWAGGER_PAGE,
        "title": spec.get("info", {}).get("title"),
        "version": spec.get("info", {}).get("version"),
        "endpoints": endpoints,
    }


def _schema_label(schema: Schema | None) -> str | None:
    if not schema:
        return None
    if "$ref" in schema:
        return class_name(schema["$ref"].rsplit("/", 1)[1])
    if schema.get("type") == "array":
        return f"list[{_schema_label(schema.get('items'))}]"
    return str(schema.get("type"))


# --------------------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------------------


def class_name(schema_name: str) -> str:
    """Strip Garmin's ``DTO``/``Dto`` suffix from a schema name."""
    if schema_name in CLASS_NAMES:
        return CLASS_NAMES[schema_name]
    return re.sub(r"(DTO|Dto)$", "", schema_name)


def snake_case(name: str) -> str:
    """Convert a Garmin ``camelCase`` field name into ``snake_case``."""
    for old, new in _SPECIAL_TOKENS:
        name = name.replace(old, new)
    # "zone1Floor" -> "zone_1Floor", but keep "spo2"/"vo2" together.
    name = re.sub(r"([a-z])(\d+)(?=[A-Z])", _split_digits, name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return name.lower()


def _split_digits(match: re.Match[str]) -> str:
    if match.string[: match.end(1)].lower().endswith(("spo", "vo")):
        return match.group(0)
    return f"{match.group(1)}_{match.group(2)}"


def attribute_names(schema_name: str, properties: dict[str, Schema]) -> dict[str, str]:
    """Map each JSON property to a unique, valid python attribute name."""
    base = {prop: FIELD_NAMES.get((schema_name, prop), snake_case(prop)) for prop in properties}
    taken = set(base.values())
    result: dict[str, str] = {}
    for prop, snake in base.items():
        attribute = snake
        # "summaryDTO" -> "summary" unless that clashes with another field.
        short = re.sub(r"_dto(?=_|$)", "", snake)
        if short not in {snake, ""} and short not in taken:
            taken.discard(snake)
            taken.add(short)
            attribute = short
        if keyword.iskeyword(attribute) or attribute in {"model_config", "model_fields"}:
            attribute += "_"
        result[prop] = attribute
    if len(set(result.values())) != len(result):
        raise SystemExit(f"duplicate attribute names in {schema_name}: {result}")
    return result


# --------------------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------------------


def _unwrap_all_of(schema: Schema) -> Schema:
    """Collapse ``{"allOf": [{"$ref": ...}], "description": ...}`` into the ``$ref``."""
    if "allOf" in schema and len(schema["allOf"]) == 1:
        merged = {**schema["allOf"][0], **{k: v for k, v in schema.items() if k != "allOf"}}
        return _unwrap_all_of(merged)
    return schema


def _timestamp_type(name: str, schema: Schema) -> str:
    lowered = name.lower()
    if "gmt" in lowered or "utc" in lowered:
        return "UtcDateTime"
    if "local" in lowered:
        return "LocalDateTime"
    if _UTC_DESCRIPTION.search(schema.get("description") or ""):
        return "UtcDateTime"
    return "LocalDateTime"


def _is_bare_object(schema: Schema) -> bool:
    return (
        schema.get("type") in {"object", None}
        and "$ref" not in schema
        and "properties" not in schema
        and "additionalProperties" not in schema
        and "items" not in schema
    )


_STRING_FORMATS = {"date": "GarminDate", "time": "GarminTime", "uuid": "UUID"}
_SIMPLE_TYPES = {"number": "float", "boolean": "bool"}


def annotation(name: str, schema: Schema) -> str:
    """Return the python annotation (without ``| None``) for a property schema."""
    schema = _unwrap_all_of(schema)
    kind = schema.get("type")
    if "$ref" in schema:
        return _ref_annotation(name, schema)
    if kind == "string":
        return _string_annotation(name, schema)
    if kind == "integer":
        return (
            "UtcDateTime"
            if _EPOCH_MS_DESCRIPTION.search(schema.get("description") or "")
            else "int"
        )
    if kind == "array":
        return _array_annotation(name, schema)
    if kind in _SIMPLE_TYPES:
        return _SIMPLE_TYPES[kind]
    return _object_annotation(name, schema)


def _ref_annotation(name: str, schema: Schema) -> str:
    target = schema["$ref"].rsplit("/", 1)[1]
    if target == "ConnectDate":
        return "GarminDate"
    if target == "ConnectTimestamp":
        return _timestamp_type(name, schema)
    if target == "LocalTime":
        return "GarminTime"
    return class_name(target)


def _string_annotation(name: str, schema: Schema) -> str:
    fmt = schema.get("format")
    description = schema.get("description") or ""
    if fmt in _STRING_FORMATS:
        return _STRING_FORMATS[fmt]
    if (
        fmt == "date-time"
        or _TIMESTAMP_PATTERN.search(f"{schema.get('pattern', '')} {schema.get('example', '')}")
        or _DATETIME_DESCRIPTION.search(description)
    ):
        return _timestamp_type(name, schema)
    if name.endswith("Date") and not re.search("time", description, re.IGNORECASE):
        # "calendarDate", "summaryDate", ...: plain YYYY-MM-DD strings without a format.
        return "GarminDate"
    return "str"


def _array_annotation(name: str, schema: Schema) -> str:
    items = _unwrap_all_of(schema.get("items", {}))
    if items.get("type") == "array" and _is_bare_object(_unwrap_all_of(items.get("items", {}))):
        return "list[SeriesRow]"
    if _is_bare_object(items):
        return "list[JsonValue]"
    return f"list[{annotation(name, items)}]"


def _object_annotation(name: str, schema: Schema) -> str:
    if isinstance(schema.get("additionalProperties"), dict):
        value = annotation(name, schema["additionalProperties"])
        return f"dict[str, {value} | None]"
    return "JsonValue"


def _references(schema: object) -> set[str]:
    found: set[str] = set()
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str):
            found.add(ref.rsplit("/", 1)[1])
        for value in schema.values():
            found |= _references(value)
    elif isinstance(schema, list):
        for value in schema:
            found |= _references(value)
    return found


def _topological(schemas: dict[str, Schema]) -> list[str]:
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(name: str) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:  # cycle: forward references resolve via model_rebuild
            return
        state[name] = 1
        for dep in sorted(_references(schemas[name])):
            if dep in schemas and dep not in SCALAR_SCHEMAS:
                visit(dep)
        state[name] = 2
        order.append(name)

    for name in sorted(schemas):
        if name not in SCALAR_SCHEMAS:
            visit(name)
    return order


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def _clean(text: str) -> str:
    # The spec double-escapes some characters ("\\u0027" for an apostrophe).
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"\s+", " ", text).strip().replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if text and not text.endswith((".", "!", "?", ")")):
        text += "."
    if text.endswith('"'):
        text += " "
    return text


def _docstring(text: str, indent: str, *, summary: str | None = None) -> list[str]:
    """Render a docstring; ``summary`` is required for class docstrings (PEP 257)."""
    text = _clean(text)
    if not text:
        return []
    width = 100 - len(indent)
    if len(text) <= width - 6:
        return [f'{indent}"""{text}"""']
    lines = textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False)
    if summary is None:  # attribute docstrings: plain wrapped text
        return [
            f'{indent}"""{lines[0]}',
            *(f"{indent}{line}" for line in lines[1:]),
            f'{indent}"""',
        ]
    sentence = re.match(r"(.+?[.!?])\s+(?=[A-Z`(])", text)
    if sentence and len(sentence.group(1)) <= width - 3:
        head, rest = sentence.group(1), text[sentence.end() :]
    else:
        head, rest = summary, text
    body = textwrap.wrap(rest, width=width, break_long_words=False, break_on_hyphens=False)
    return [f'{indent}"""{head}', "", *(f"{indent}{line}" for line in body), f'{indent}"""']


def _field_doc(schema: Schema) -> str:
    schema = _unwrap_all_of(schema)
    parts = [schema.get("description") or ""]
    if schema.get("enum"):
        parts.append("Known values: " + ", ".join(f"``{value}``" for value in schema["enum"]) + ".")
    elif schema.get("type") == "array" and _unwrap_all_of(schema.get("items", {})).get("enum"):
        values = _unwrap_all_of(schema["items"])["enum"]
        parts.append("Known values: " + ", ".join(f"``{value}``" for value in values) + ".")
    if schema.get("deprecated"):
        parts.append("Deprecated.")
    return " ".join(part.strip().rstrip(".") + "." for part in parts if part.strip())


def render_model(schema_name: str, schema: Schema) -> list[str]:
    """Render one pydantic model class."""
    name = class_name(schema_name)
    properties: dict[str, Schema] = schema.get("properties", {})
    bases = f"{MIXINS[schema_name]}, GarminModel" if schema_name in MIXINS else "GarminModel"
    lines = [f"class {name}({bases}):"]
    fallback = f"Garmin ``{schema_name}`` object."
    lines += _docstring(schema.get("description") or fallback, "    ", summary=fallback)
    if not properties:
        return lines
    lines.append("")
    attributes = attribute_names(schema_name, properties)
    for prop, prop_schema in properties.items():
        attribute = attributes[prop]
        ann = annotation(prop, prop_schema)
        if (schema_name, prop) in REQUIRED_FIELDS:
            lines.append(f'    {attribute}: {ann} = Field(alias="{prop}")')
        elif attribute != prop:
            lines.append(f'    {attribute}: {ann} | None = Field(default=None, alias="{prop}")')
        else:
            lines.append(f"    {attribute}: {ann} | None = None")
        lines += _docstring(_field_doc(prop_schema), "    ")
    return lines


HEADER = '''"""Pydantic models for Garmin Connect Personal API responses.

Generated by ``scripts/generate_models.py`` from Garmin's OpenAPI document, with
corrections where the live API disagrees with it. Do not edit by hand.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field, JsonValue

from ._base import (
    GarminDate,
    GarminModel,
    GarminTime,
    LocalDateTime,
    SeriesRow,
    UtcDateTime,
)
from ._series import BodyBatterySeries, HeartRateSeries, SpO2Series
'''


def render(spec: Schema) -> tuple[str, list[str]]:
    """Render the generated module and return it with the exported class names."""
    schemas: dict[str, Schema] = spec["components"]["schemas"]
    order = _topological(schemas)
    names = [class_name(name) for name in order]
    if len(set(names)) != len(names):
        raise SystemExit("class name collision after stripping DTO suffixes")
    body: list[str] = [HEADER, "__all__ = ["]
    body += [f'    "{name}",' for name in sorted(names)]
    body.append("]")
    for schema_name in order:
        body += ["", ""]
        body += render_model(schema_name, schemas[schema_name])
    body.append("")
    return "\n".join(body), sorted(names)


INIT_TEMPLATE = '''"""Typed response models.

Every model subclasses :class:`GarminModel`: ``snake_case`` attributes with Garmin's
``camelCase`` names as aliases, all fields optional, and unknown fields preserved.
"""

from ._base import GarminDate, GarminModel, GarminTime, LocalDateTime, SeriesRow, UtcDateTime
from ._series import (
    BodyBatterySample,
    HeartRateSample,
    MonitoringEnvironmentSample,
    SpO2Sample,
)
from ._generated import (
{imports}
)

__all__ = [
{exports}
]
'''


def render_init(names: list[str]) -> str:
    """Render ``models/__init__.py`` re-exporting every model."""
    base = [
        "BodyBatterySample",
        "GarminDate",
        "GarminModel",
        "GarminTime",
        "HeartRateSample",
        "LocalDateTime",
        "MonitoringEnvironmentSample",
        "SeriesRow",
        "SpO2Sample",
        "UtcDateTime",
    ]
    return INIT_TEMPLATE.format(
        imports="\n".join(f"    {name}," for name in names),
        exports="\n".join(f'    "{name}",' for name in sorted([*base, *names])),
    )


def main(spec: str | None = None, dump_spec: str | None = None) -> None:
    """Regenerate the models.

    Args:
        spec: Path to a local OpenAPI JSON file. Downloads the current spec if omitted.
        dump_spec: Also write the (unpatched) spec to this path, for diffing.
    """
    raw: Schema = json.loads(Path(spec).read_text()) if spec else fetch_spec()
    if dump_spec:
        Path(dump_spec).write_text(json.dumps(raw, indent=2) + "\n")
    patched = patch_spec(raw)
    source, names = render(patched)
    surface = ROOT / "spec" / "endpoints.json"
    surface.parent.mkdir(exist_ok=True)
    surface.write_text(json.dumps(endpoint_surface(patched), indent=2) + "\n")
    generated = MODELS_DIR / "_generated.py"
    init = MODELS_DIR / "__init__.py"
    generated.write_text(source)
    init.write_text(render_init(names))
    subprocess.run([sys.executable, "-m", "ruff", "format", str(generated), str(init)], check=True)
    subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--fix", "--quiet", str(generated), str(init)],
        check=False,
    )
    print(f"wrote {len(names)} models to {generated.relative_to(ROOT)}")
    print(f"wrote the endpoint surface to {surface.relative_to(ROOT)}")


if __name__ == "__main__":
    fire.Fire(main)
