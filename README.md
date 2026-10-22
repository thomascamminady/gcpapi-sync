# gcpapi-sync

[![PyPI](https://img.shields.io/pypi/v/gcpapi-sync)](https://pypi.org/project/gcpapi-sync/)
[![Python](https://img.shields.io/pypi/pyversions/gcpapi-sync)](https://pypi.org/project/gcpapi-sync/)
[![CI](https://github.com/thomascamminady/gcpapi-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/thomascamminady/gcpapi-sync/actions/workflows/ci.yml)
[![API drift](https://github.com/thomascamminady/gcpapi-sync/actions/workflows/drift.yml/badge.svg)](https://github.com/thomascamminady/gcpapi-sync/actions/workflows/drift.yml)
[![Docs](https://img.shields.io/badge/docs-pdoc-blue)](https://thomascamminady.github.io/gcpapi-sync/)
![Typed](https://img.shields.io/badge/typing-strict-brightgreen)

A typed Python client and command line tool for the
[Garmin Connect Personal API](https://developer.garmin.com/gc-developer-program/personal/personal-faq/),
the official API that gives you programmatic access to **your own** Garmin data:
activities and their FIT files, sleep, heart rate, HRV, stress, Body Battery, SpO2,
steps, weight, nutrition and more.

> **Disclaimer.** This is a personal, private project. It is not affiliated with,
> endorsed by, or developed on behalf of my employer, Wahoo Fitness, and it is not
> affiliated with or endorsed by Garmin. "Garmin" and "Garmin Connect" are trademarks
> of Garmin Ltd. or its subsidiaries. Use of the API is subject to
> [Garmin's terms](https://www.garmin.com/en-US/legal/terms-of-use/).

## Highlights

- **Every endpoint**, as typed methods on a sync `GarminClient` and an async
  `AsyncGarminClient` with identical signatures.
- **109 pydantic models**, generated from Garmin's OpenAPI document and *corrected
  where the live API contradicts it* (see [API quirks](https://github.com/thomascamminady/gcpapi-sync#api-quirks)). No `dict`, no
  `Any`: maps with known keys are models, intraday series have typed accessors.
- **Strictly typed** (`py.typed`), checked with `ty` (every rule as an error) and
  `mypy --strict`, with no suppression comments anywhere in the code base.
- **Authentication handled for you**: API key discovery, the 24-hour token exchange,
  caching (in memory or on disk, shared with Garmin's own client), renewal before
  expiry and after a rejected token.
- **Resilient**: retries with exponential backoff and jitter, `Retry-After` support,
  and a precise exception hierarchy.
- **No server limits to remember**: date ranges of any length are split to fit each
  endpoint (concurrently with the async client); searches of any size are paged.
- **CLI** with everyday commands (`today`, `activities`, incremental FIT `sync`,
  `export`) plus raw JSON for every endpoint.
- **Kept in sync with Garmin**: a weekly job regenerates the models from Garmin's
  published spec and opens a pull request when anything changes.

## Installation

```bash
uv add gcpapi-sync          # in a uv project
pip install gcpapi-sync     # with pip
uv tool install gcpapi-sync # just the gcpapi command
uvx --from gcpapi-sync gcpapi today   # run the CLI without installing
```

Requires Python 3.11 or newer. API documentation:
[thomascamminady.github.io/gcpapi-sync](https://thomascamminady.github.io/gcpapi-sync/).

## Getting an API key

1. In [Garmin Connect](https://connect.garmin.com) open **Settings → Personal API**
   and create a key. Garmin shows it only once; keys expire after 30 days to a year.
2. Store it:

   ```bash
   gcpapi setup    # prompts for the key (hidden), verifies it, saves it with mode 600
   ```

The client looks for the key in this order: the `api_key` argument, the `key_file`
argument, the `GARMIN_PERSONAL_API_KEY` environment variable, and
`~/.config/garmin/connect-personal-api-key` (where `gcpapi setup` and Garmin's own
client put it). Never commit the key; this repository's pre-commit hooks refuse to.

## Quick start

```python
from gcpapi_sync import GarminClient

with GarminClient() as garmin:
    for run in garmin.search_activities(limit=5, activity_type="running"):
        km = (run.distance or 0) / 1000
        print(f"{run.start_time_local:%Y-%m-%d}  {run.activity_name}  {km:.1f} km")

    sleep = garmin.get_daily_sleep("2026-10-21")
    if sleep.sleep_scores and sleep.sleep_scores.overall:
        print("sleep score", sleep.sleep_scores.overall.value)

    steps = garmin.get_daily_steps("2026-01-01", "2026-10-21")  # 14 requests, one list
```

The async client has the same methods as coroutines and fetches the windows of long
date ranges concurrently:

```python
import asyncio
from gcpapi_sync import AsyncGarminClient


async def main() -> None:
    async with AsyncGarminClient(max_concurrency=4) as garmin:
        sleep, hrv, steps = await asyncio.gather(
            garmin.get_daily_sleep_summaries("2026-01-01", "2026-10-21"),
            garmin.get_daily_hrv_summaries("2026-01-01", "2026-10-21"),
            garmin.get_daily_steps("2026-01-01", "2026-10-21"),
        )
        async for activity in garmin.iter_activities(start_date="2026-10-01"):
            print(activity.activity_id, activity.activity_name)


asyncio.run(main())
```

## Usage

### Endpoints

Dates are `datetime.date` objects or `YYYY-MM-DD` strings; ranges are inclusive.

| Method | Returns | Garmin's limit per request |
| --- | --- | --- |
| `search_activities(start=0, limit=20, **filters)` | `list[SummarizedActivity]` | ~45 per page (paged for you) |
| `iter_activities(page_size=20, **filters)` | iterator of `SummarizedActivity` | |
| `get_activity(activity_id)` | `ActivitySummary` | |
| `get_activity_workouts(activity_id)` | `list[ActivityWorkout]` | |
| `download_activity(activity_id)` | `ActivityFile` (original FIT/GPX/TCX) | |
| `download_activity_zip(activity_id)` | `bytes` (Garmin's zip) | |
| `get_daily_summary(day)` | `UserDailySummary` | |
| `get_daily_summaries(start, end)` | `list[UserDailySummaryV2]` | 31 days (split for you) |
| `get_daily_sleep(day)` | `DailySleep` | |
| `get_daily_sleep_summaries(start, end)` | `list[DailySleep]` | 31 days (split for you) |
| `get_daily_heart_rate(day)` | `DailyHeartRate` | |
| `get_heart_rate_zones()` | `list[HeartRateZones]` | |
| `get_daily_hrv(day)` | `HrvChart \| None` | |
| `get_daily_hrv_summaries(start, end)` | `list[HrvSummary]` | 365 days (split for you) |
| `get_daily_stress(start, end)` | `list[MultiValueWellnessChartItem]` | 28 days (split for you) |
| `get_daily_steps(start, end)` | `list[StepsDailyStat]` | 28 days (split for you) |
| `get_daily_hydration(start, end)` | `list[UserDailyHydration]` | 31 days (split for you) |
| `get_daily_body_battery(start, end, chunks=None)` | `list[BodyBatteryDailyReport]` | 31 days (split for you) |
| `get_daily_spo2(day)` | `DailySpO2AndAcclimation` | |
| `get_fitness_age(day)` | `FitnessAge` | |
| `get_weight_range(start, end, include_all=False)` | `WeightRange` (weights in grams) | about a year |
| `get_weight_entries(start, end)` | `list[DateWeight]` | currently broken server-side |
| `get_effective_weight_goals(start, end)` | `UserGoalsRange` | |
| `get_daily_food_logs(day)` | `DailyNutritionSummary` | |
| `get_menstrual_cycle_summary(day)` | `MenstrualCycleSummary \| None` | |
| `get_menstrual_cycle_day_view(day, today=None)` | `DetailedMenstrualCycleDay \| None` | |
| `get_wellness_activity_summaries(start, end)` | `list[WellnessActivitySummary]` | |
| `get_user_profile_settings()` | `UserSettings` | |
| `get_json(path, params)` | decoded JSON, for endpoints not wrapped yet | |

### Searching activities

`search_activities` and `iter_activities` accept every filter the API supports as
typed keyword arguments (see `ActivityFilters`): dates, activity and event types,
text search, favorites, distance/duration/elevation/power ranges, a geographic
`BoundingBox`, dive parameters, sorting and more.

```python
from gcpapi_sync import BoundingBox

long_runs = garmin.search_activities(
    limit=100,
    activity_type="running",
    min_distance=21_000,  # meters
    start_date="2026-01-01",
    bounding_box=BoundingBox(north=50.8, east=7.2, south=50.6, west=7.0),
    sort_by="distance",
)

for activity in garmin.iter_activities(activity_type="cycling"):
    garmin.download_activity(activity.activity_id).save("~/garmin/fit")
```

### Models

Every response is a pydantic model (`gcpapi_sync.models`):

- attributes are `snake_case`; Garmin's `camelCase` names are the aliases
  (`model_dump(by_alias=True)` reproduces Garmin's JSON);
- every field is optional, because Garmin omits and nulls fields freely;
- unknown fields are kept in `model_extra`, so API additions never break parsing;
- timestamps are `datetime` objects: timezone-aware UTC for Garmin's `*GMT` fields
  and naive wall-clock time for `*Local` fields, whether Garmin sent ISO strings or
  epoch milliseconds; calendar days are `date`, times of day are `time`.

Intraday series come as compact rows; typed accessors resolve their columns:

```python
heart_rate = garmin.get_daily_heart_rate("2026-10-21")
for sample in heart_rate.heart_rate_samples():  # list[HeartRateSample]
    print(sample.timestamp, sample.heart_rate)

report = garmin.get_daily_body_battery("2026-10-21", "2026-10-21")[0]
levels = report.body_battery_samples()  # list[BodyBatterySample]
spo2 = garmin.get_daily_spo2("2026-10-21").spo2_hourly_samples()
```

### Errors

```text
GarminError
├── ConfigurationError            no API key found, invalid options
└── APIError
    ├── APIConnectionError        network failure (after retries)
    │   └── APITimeoutError
    ├── APIStatusError            non-2xx response (after retries); .status_code, .body, .error_id
    │   ├── BadRequestError       400
    │   ├── AuthenticationError   401: key or token invalid, expired or revoked
    │   ├── PermissionDeniedError 403
    │   ├── NotFoundError         404
    │   ├── RateLimitError        429; .retry_after
    │   └── InternalServerError   5xx
    └── ResponseValidationError   a 2xx response that does not match the models; .raw
```

Invalid arguments (malformed dates, reversed ranges, unknown filters) raise
`ValueError`/`TypeError` before any request is made.

### Configuration

```python
import httpx
from gcpapi_sync import FileTokenCache, GarminClient, RetryConfig

garmin = GarminClient(
    key_file="~/secrets/garmin.key",
    token_cache=FileTokenCache(),  # reuse tokens across processes
    timeout=60,
    retry=RetryConfig(max_retries=5, max_delay=30),
    http_client=httpx.Client(proxy="http://proxy:3128"),  # you close it
)
```

Implement the `TokenCache` protocol to keep tokens elsewhere (a keyring, Redis, ...).
Logs go to the `gcpapi_sync` logger (requests at `DEBUG`, retries at `INFO`); keys and
tokens are never logged or shown in `repr`.

## Command line

```bash
gcpapi setup                                    # store and verify your API key
gcpapi status                                   # key source, token validity
gcpapi today                                    # steps, HR, stress, Body Battery, sleep, HRV, activities
gcpapi today 2026-10-21 --as_json
gcpapi activities --limit 20 --activity_type running
gcpapi sync ~/garmin/fit --since 2026-01-01     # download new FIT files only
gcpapi export 2026-01-01 2026-10-21 ~/garmin/health
gcpapi get_daily_sleep 2026-10-21               # any client method, as JSON
gcpapi search_activities --limit 5 --min_distance 10000
gcpapi logout                                   # delete the cached token
```

Options go after the command (`gcpapi status --no_cache`). `gcpapi -- --help` and
`gcpapi <command> -- --help` describe everything; `python -m gcpapi_sync` works too.

## API quirks

The client hides a number of places where Garmin's service disagrees with its own
documentation (verified against the live API in October 2026):

- Timestamps are documented as strings but several arrive as epoch milliseconds
  (sleep, weight); several documented objects arrive as plain strings (`ConnectDate`,
  `ConnectTimestamp`, `LocalTime`); the fitness-age endpoint returns an object, not
  the documented array; intraday series are arrays of numbers, not objects.
- Fields documented as non-nullable are frequently `null`, and some undocumented
  fields are returned; both are modeled.
- Range limits differ from the documentation: daily summaries fail with HTTP 500 for
  ranges over roughly 90 days although a year is documented; activity searches fail
  with HTTP 500 for pages over about 45 activities, *including the server's own
  default page size*.
- `GET /weight-service/weight/all/...` answers HTTP 400 for every input.
- Activity downloads require `Accept: application/x-zip-compressed`.

## Development

```bash
git clone git@github.com:thomascamminady/gcpapi-sync.git && cd gcpapi-sync
uv sync                         # environment with all dev tools
uv run pre-commit install       # ruff, ty, mypy, secret scanning on commit; tests on push
uv run pytest                   # unit tests (no network)
uv run pytest -m live           # against the real API with your key
uv run pre-commit run --all-files
uv run --group docs pdoc gcpapi_sync gcpapi_sync.exceptions gcpapi_sync.cli --docformat google
```

Models are generated; never edit `src/gcpapi_sync/models/_generated.py` by hand.
Corrections for the live API live in `scripts/generate_models.py`:

```bash
uv run scripts/generate_models.py                 # fetch Garmin's current spec and regenerate
uv run scripts/scrub_fixture.py response.json tests/fixtures/name.json   # anonymize a fixture
```

See [CONTRIBUTING.md](https://github.com/thomascamminady/gcpapi-sync/blob/main/CONTRIBUTING.md)
for the full workflow and how API drift is detected.

## License

[MIT](https://github.com/thomascamminady/gcpapi-sync/blob/main/LICENSE)
