# /// script
# requires-python = ">=3.11"
# dependencies = ["fire>=0.7"]
# ///
"""Turn a real API response into an anonymous test fixture.

The structure (keys, nesting, types, nulls) is kept exactly; every value that could
identify a person is replaced deterministically:

* numbers get new values of the same type and magnitude (single-digit integers,
  such as Garmin's negative "no data" sentinels, and column indexes are kept);
  epoch-millisecond timestamps become increasing synthetic timestamps,
* dates, times and timestamps become fixed synthetic values in the same format,
* strings are replaced unless they are enum-like constants (``EXCELLENT``) or the
  value of an allow-listed key such as ``typeKey``,
* lists are truncated.

Usage::

    uv run scripts/scrub_fixture.py response.json tests/fixtures/sleep.json

Always review the output before committing it.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import fire

JsonValue = dict[str, "JsonValue"] | list["JsonValue"] | str | int | float | bool | None

_SYNTHETIC_EPOCH_MS = 1_768_464_000_000  # 2026-01-15T08:00:00Z
_ENUM = re.compile(r"^[A-Z][A-Z0-9_]*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(?P<fraction>\.\d+)?(?P<zone>Z|[+-]\d{2}:?\d{2})?$"
)
_TIME = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_FIXED_FORMATS = (
    (_DATE, "2026-01-15"),
    (_TIME, "08:00:00"),
    (_UUID, "00000000-0000-4000-8000-000000000000"),
)
# Keys whose string values are vocabulary, not personal data.
_SAFE_KEYS = frozenset(
    {
        "typeKey",
        "key",
        "sourceType",
        "qualifierKey",
        "status",
        "bodyBatteryValueDescriptorKey",
        "spo2ValueDescKey",
        "spo2ValueDescriptorKey",
        "monEnvValueDescKey",
        "sport",
        "trainingMethod",
        "measurementSystem",
        "unitKey",
        "privacy",
        "typeKeyName",
        "lactateThresholdHeartRateSource",
    }
)


class _Scrubber:
    def __init__(self, max_items: int) -> None:
        self.max_items = max_items
        self.timestamp_counter = 0

    def value(self, value: JsonValue, key: str, path: str) -> JsonValue:
        if isinstance(value, dict):
            return {name: self.value(item, name, f"{path}.{name}") for name, item in value.items()}
        if isinstance(value, list):
            return [self.value(item, key, f"{path}[]") for item in value[: self.max_items]]
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int):
            # Column indexes are structure, not data.
            return value if key.lower().endswith("index") else self.integer(value, path)
        if isinstance(value, float):
            return self.number(value, path)
        return self.string(value, key)

    def integer(self, value: int, path: str) -> int:
        if -10 < value < 10:
            return value
        if value > 1_000_000_000_000:
            self.timestamp_counter += 1
            return _SYNTHETIC_EPOCH_MS + self.timestamp_counter * 60_000
        digits = len(str(abs(value)))
        low = int("1" + "0" * (digits - 1))
        high = int("1" + "0" * digits)
        return low + _hash(path, value) % (high - low)

    @staticmethod
    def number(value: float, path: str) -> float:
        if value == 0:
            return 0.0
        scale = max(abs(value), 1.0)
        fraction = (_hash(path, value) % 1000) / 1000
        return round((0.5 + fraction) * scale * (1 if value > 0 else -1), 1)

    @staticmethod
    def string(value: str, key: str) -> str:
        if match := _DATETIME.match(value):
            separator = "T" if "T" in value else " "
            return f"2026-01-15{separator}08:00:00{match['fraction'] or ''}{match['zone'] or ''}"
        for pattern, replacement in _FIXED_FORMATS:
            if pattern.match(value):
                return replacement[: len(value)] if pattern is _TIME else replacement
        if _ENUM.match(value) or key in _SAFE_KEYS:
            return value
        if value.startswith(("http://", "https://")):
            return "https://example.invalid/redacted"
        return "redacted"


def _hash(path: str, value: float) -> int:
    return int.from_bytes(hashlib.sha256(f"{path}={value}".encode()).digest()[:8], "big")


def scrub(source: str, target: str, *, max_items: int = 3) -> None:
    """Write an anonymized copy of the JSON file ``source`` to ``target``.

    Args:
        source: A raw API response.
        target: Where to write the fixture.
        max_items: Keep at most this many elements of every list.
    """
    data: JsonValue = json.loads(Path(source).read_text(encoding="utf-8"))
    cleaned = _Scrubber(max_items).value(data, "", "$")
    Path(target).write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")
    kept = sorted(_strings(cleaned) - {"redacted", "https://example.invalid/redacted"})
    print(f"wrote {target}; strings kept verbatim (review them): {kept}")


def _strings(value: JsonValue) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_strings(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_strings(item) for item in value))
    if isinstance(value, str) and not re.match(r"^\d{4}-\d{2}-\d{2}|^\d{2}:\d{2}|^0{8}-", value):
        return {value}
    return set()


if __name__ == "__main__":
    fire.Fire(scrub)
