"""Command line interface: ``gcpapi <command> [arguments]``.

Everyday commands::

    gcpapi setup                      # store your API key (prompted, never echoed)
    gcpapi status                     # where the key comes from, token validity
    gcpapi today                      # steps, sleep, HRV, Body Battery, activities
    gcpapi activities --limit 20      # recent activities as a table
    gcpapi sync ~/garmin/fit          # download new activity files (FIT) incrementally
    gcpapi export 2026-01-01 2026-10-21 ~/garmin/health   # health data as JSON files

Every method of :class:`gcpapi_sync.GarminClient` is also a command that prints its
result as JSON (Garmin's ``camelCase`` names, ``null`` fields omitted)::

    gcpapi get_daily_sleep 2026-10-21
    gcpapi search_activities --limit 5 --activity_type running

Global flags go after the command (``gcpapi status --no_cache``): ``--key_file``,
``--api_key``, ``--base_url``, ``--no_cache`` (skip the on-disk token cache) and
``--indent``.
"""

from __future__ import annotations

import functools
import getpass
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Unpack

import fire
from pydantic import BaseModel, Field, TypeAdapter

from ._auth import (
    FileTokenCache,
    MemoryTokenCache,
    file_is_private,
    locate_api_key,
    save_api_key,
)
from ._client import GarminClient
from ._constants import DEFAULT_API_KEY_FILE, DEFAULT_TOKEN_CACHE_FILE
from ._dates import to_date
from .exceptions import GarminError, NotFoundError
from .models import DailySleep, HrvChart, SummarizedActivity, UserDailySummary

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ._dates import DateLike
    from ._endpoints import ActivityFilters
    from .models import GarminModel

__all__ = ["Cli", "main"]

_JSON: TypeAdapter[object] = TypeAdapter(object)

# Commands with their own implementation; every other public client method gets a
# generic wrapper that prints the result as JSON.
_CUSTOM_COMMANDS = frozenset(
    {"close", "download_activity", "download_activity_zip", "get_access_token", "iter_activities"}
)

_EXPORTS: dict[str, Callable[[GarminClient, DateLike, DateLike], Sequence[GarminModel]]] = {
    "daily_summaries": GarminClient.get_daily_summaries,
    "sleep": GarminClient.get_daily_sleep_summaries,
    "hrv": GarminClient.get_daily_hrv_summaries,
    "stress": GarminClient.get_daily_stress,
    "steps": GarminClient.get_daily_steps,
    "hydration": GarminClient.get_daily_hydration,
    "body_battery": GarminClient.get_daily_body_battery,
    "wellness_activities": GarminClient.get_wellness_activity_summaries,
}


class TodayReport(BaseModel):
    """Everything ``gcpapi today`` shows, for ``--as_json``."""

    day: date
    summary: UserDailySummary
    sleep: DailySleep
    hrv: HrvChart | None
    activities: list[SummarizedActivity]


class SyncReport(BaseModel):
    """Outcome of ``gcpapi sync``."""

    directory: str
    downloaded: list[str] = Field(default_factory=list)
    skipped: int = 0
    unavailable: list[int] = Field(default_factory=list)


def _write(text: str) -> None:
    sys.stdout.write(text + "\n")


def _note(text: str) -> None:
    sys.stderr.write(text + "\n")


def _today() -> date:
    return datetime.now(UTC).astimezone().date()


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes, secs = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _km(meters: float | None) -> str:
    return "-" if meters is None or meters == 0 else f"{meters / 1000:.2f} km"


def _number(value: float | None, unit: str = "") -> str:
    return "-" if value is None else f"{value:,.0f}{unit}"


def _activity_type(activity: SummarizedActivity) -> str:
    key = activity.activity_type.type_key if activity.activity_type is not None else None
    return (key or "activity").replace("_", " ")


class Cli:
    """Garmin Connect Personal API from the command line.

    Args:
        api_key: Personal API key. Prefer ``gcpapi setup``, a key file or the
            GARMIN_PERSONAL_API_KEY variable: command lines are visible to other users.
        key_file: File containing the API key.
        base_url: Alternative API host.
        no_cache: Neither read nor write the on-disk token cache.
        indent: JSON indentation for JSON output; 0 prints a single line.
    """

    def __init__(
        self,
        api_key: str | None = None,
        key_file: str | None = None,
        base_url: str | None = None,
        *,
        no_cache: bool = False,
        indent: int = 2,
    ) -> None:
        self._api_key = api_key
        self._key_file = key_file
        self._base_url = base_url
        self._no_cache = no_cache
        self._indent = indent if indent > 0 else None
        self._garmin: GarminClient | None = None

    @property
    def _client(self) -> GarminClient:
        if self._garmin is None:
            self._garmin = GarminClient(
                self._api_key,
                key_file=self._key_file,
                base_url=self._base_url,
                token_cache=MemoryTokenCache() if self._no_cache else FileTokenCache(),
            )
        return self._garmin

    def _print_json(self, value: object) -> None:
        dumped = _JSON.dump_json(value, by_alias=True, exclude_none=True, indent=self._indent)
        _write(dumped.decode())

    # -- account -----------------------------------------------------------------------

    def setup(self, *, overwrite: bool = False) -> None:
        """Store your Personal API key in ~/.config/garmin/connect-personal-api-key.

        The key is read from a hidden prompt (or from standard input when piped),
        checked against Garmin, and saved readable only by you. Create a key in
        Garmin Connect under Settings > Personal API.

        Args:
            overwrite: Replace an existing key file.
        """
        target = DEFAULT_API_KEY_FILE.expanduser()
        if target.exists() and not overwrite:
            raise FileExistsError(f"{target} already exists; pass --overwrite to replace it")
        if sys.stdin.isatty():
            key = getpass.getpass("Garmin Personal API key (input hidden): ")
        else:
            key = sys.stdin.readline()
        token = GarminClient(
            key,
            base_url=self._base_url,
            token_cache=MemoryTokenCache() if self._no_cache else FileTokenCache(),
        ).get_access_token()
        saved = save_api_key(key, target, overwrite=overwrite)
        expiry = f"{token.expires_at:%Y-%m-%d %H:%M} UTC"
        _write(f"Key verified and saved to {saved} (token valid until {expiry})")

    def status(self) -> None:
        """Show where the API key comes from and whether it currently works."""
        source = locate_api_key(self._api_key, key_file=self._key_file)
        private = source.path is None or file_is_private(source.path)
        _write(f"API host      {self._client.base_url}")
        _write(
            f"API key       {source.describe()}"
            + ("" if private else " (WARNING: readable by others)")
        )
        cache = "disabled" if self._no_cache else str(DEFAULT_TOKEN_CACHE_FILE.expanduser())
        _write(f"Token cache   {cache}")
        token = self._client.get_access_token()
        remaining = token.expires_at - datetime.now(UTC)
        hours, minutes = divmod(int(remaining.total_seconds()) // 60, 60)
        _write(
            f"Access token  valid until {token.expires_at:%Y-%m-%d %H:%M} UTC "
            f"({hours}h {minutes:02d}m left)"
        )

    def logout(self, *, forget_key: bool = False) -> None:
        """Delete the cached access token.

        Args:
            forget_key: Also delete the stored API key file.
        """
        FileTokenCache().clear()
        _write(f"Removed cached token {DEFAULT_TOKEN_CACHE_FILE.expanduser()}")
        if forget_key:
            DEFAULT_API_KEY_FILE.expanduser().unlink(missing_ok=True)
            _write(f"Removed API key {DEFAULT_API_KEY_FILE.expanduser()}")

    def token(self) -> None:
        """Obtain an access token and print when it expires (the token is not shown)."""
        token = self._client.get_access_token()
        self._print_json({"expiresAt": token.expires_at})

    # -- overviews ---------------------------------------------------------------------

    def today(self, day: str | None = None, *, as_json: bool = False) -> None:
        """Summarize a day: steps, heart rate, stress, Body Battery, sleep, HRV, activities.

        Args:
            day: The day to show (YYYY-MM-DD); defaults to today.
            as_json: Print the underlying data as JSON instead.
        """
        when = to_date(day, "day") if day is not None else _today()
        report = TodayReport(
            day=when,
            summary=self._client.get_daily_summary(when),
            sleep=self._client.get_daily_sleep(when),
            hrv=self._client.get_daily_hrv(when),
            activities=self._client.search_activities(start_date=when, end_date=when, limit=20),
        )
        if as_json:
            self._print_json(report)
            return
        for line in _render_today(report):
            _write(line)

    def activities(self, limit: int = 10, **filters: Unpack[ActivityFilters]) -> None:
        """List recent activities as a table.

        Args:
            limit: How many activities to show.
            **filters: Any activity filter, such as --activity_type running or
                --start_date 2026-10-01.
        """
        rows = [("Start", "Type", "Name", "Distance", "Duration", "Avg HR", "ID")]
        for activity in self._client.search_activities(limit=limit, **filters):
            start = activity.start_time_local
            rows.append(
                (
                    f"{start:%Y-%m-%d %H:%M}" if start is not None else "-",
                    _activity_type(activity),
                    (activity.activity_name or "")[:32],
                    _km(activity.distance),
                    _duration(activity.duration),
                    _number(activity.average_hr),
                    str(activity.activity_id or ""),
                )
            )
        widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
        for row in rows:
            _write(
                "  ".join(
                    cell.ljust(width) for cell, width in zip(row, widths, strict=True)
                ).rstrip()
            )

    # -- bulk --------------------------------------------------------------------------

    def sync(
        self,
        output: str = ".",
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int | None = None,
        metadata: bool = False,
        **filters: Unpack[ActivityFilters],
    ) -> None:
        """Download every activity's original file (usually FIT) not yet in ``output``.

        Files are named like Garmin names them (``<activityId>_ACTIVITY.fit``); activities
        whose file already exists are skipped, so running it again only fetches new ones.

        Args:
            output: Target directory (created if needed).
            since: Only activities starting on or after this date.
            until: Only activities starting on or before this date.
            limit: Stop after this many downloads.
            metadata: Also save each activity's summary as ``<activityId>.json``.
            **filters: Any activity filter, such as --activity_type running.
        """
        directory = Path(output).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        have = {path.name.split("_", 1)[0] for path in directory.iterdir() if path.is_file()}
        report = SyncReport(directory=str(directory))
        if since is not None:
            filters["start_date"] = since
        if until is not None:
            filters["end_date"] = until
        for activity in self._client.iter_activities(**filters):
            if limit is not None and len(report.downloaded) >= limit:
                break
            activity_id = activity.activity_id
            if str(activity_id) in have:
                report.skipped += 1
                continue
            try:
                saved = self._client.download_activity(activity_id).save(directory)
            except NotFoundError:
                report.unavailable.append(activity_id)
                continue
            except FileExistsError:
                report.skipped += 1
                continue
            if metadata:
                summary = activity.model_dump_json(by_alias=True, exclude_none=True, indent=2)
                (directory / f"{activity_id}.json").write_text(summary + "\n", encoding="utf-8")
            report.downloaded.append(saved.name)
            _note(f"downloaded {saved.name}")
        self._print_json(report)

    def export(self, start: str, end: str, output: str = ".") -> None:
        """Save daily health data for a date range as JSON files in ``output``.

        Writes daily_summaries, sleep, hrv, stress, steps, hydration, body_battery and
        wellness_activities (one ``.json`` array each).

        Args:
            start: First day (YYYY-MM-DD).
            end: Last day (YYYY-MM-DD).
            output: Target directory (created if needed).
        """
        directory = Path(output).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        written: dict[str, int] = {}
        for name, fetch in _EXPORTS.items():
            items = fetch(self._client, start, end)
            content = _JSON.dump_json(items, by_alias=True, exclude_none=True, indent=2)
            (directory / f"{name}.json").write_bytes(content + b"\n")
            written[name] = len(items)
            _note(f"{name}: {len(items)} records")
        self._print_json({"directory": str(directory), "records": written})

    # -- raw commands that need special output -----------------------------------------

    def download_activity(
        self, activity_id: int, output: str = ".", *, overwrite: bool = False
    ) -> None:
        """Save one activity's original file (usually FIT) and print its path."""
        saved = self._client.download_activity(activity_id).save(output, overwrite=overwrite)
        self._print_json({"path": str(saved)})

    def download_activity_zip(
        self, activity_id: int, output: str = ".", *, overwrite: bool = False
    ) -> None:
        """Save the zip archive Garmin serves for an activity and print its path."""
        target = Path(output).expanduser()
        if target.is_dir():
            target /= f"{activity_id}.zip"
        if target.exists() and not overwrite:
            raise FileExistsError(f"{target} already exists; pass --overwrite to replace it")
        target.write_bytes(self._client.download_activity_zip(activity_id))
        self._print_json({"path": str(target)})

    def iter_activities(self, page_size: int = 20, **filters: Unpack[ActivityFilters]) -> None:
        """Print every matching activity as one JSON object per line."""
        for activity in self._client.iter_activities(page_size=page_size, **filters):
            _write(activity.model_dump_json(by_alias=True, exclude_none=True))


def _json_command(name: str) -> Callable[..., None]:
    method = getattr(GarminClient, name)

    @functools.wraps(method)
    def command(self: Cli, *args: object, **kwargs: object) -> None:
        self._print_json(getattr(self._client, name)(*args, **kwargs))

    return command


def _render_today(report: TodayReport) -> list[str]:
    summary, sleep, hrv = report.summary, report.sleep, report.hrv
    lines = [f"{report.day:%A, %Y-%m-%d}"]

    def add(label: str, value: str) -> None:
        lines.append(f"  {label:<13} {value}")

    add("Steps", f"{_number(summary.total_steps)} of {_number(summary.daily_step_goal)}")
    add("Distance", _km(summary.total_distance_meters))
    add(
        "Resting HR",
        f"{_number(summary.resting_heart_rate, ' bpm')} "
        f"(7-day {_number(summary.last_seven_days_avg_resting_heart_rate)})",
    )
    add(
        "Stress",
        f"avg {_number(summary.average_stress_level)}, max {_number(summary.max_stress_level)}",
    )
    add(
        "Body Battery",
        f"{_number(summary.body_battery_lowest_value)} to "
        f"{_number(summary.body_battery_highest_value)}, "
        f"now {_number(summary.body_battery_most_recent_value)}",
    )
    score = sleep.sleep_scores.overall if sleep.sleep_scores is not None else None
    score_text = (
        f", score {_number(score.value)} {score.qualifier_key or ''}".rstrip()
        if score is not None
        else ""
    )
    add("Sleep", f"{_duration(sleep.sleep_time_seconds)} h{score_text}")
    if hrv is not None and hrv.hrv_summary is not None:
        add(
            "HRV",
            f"{_number(hrv.hrv_summary.last_night_avg, ' ms')} last night, weekly "
            f"{_number(hrv.hrv_summary.weekly_avg, ' ms')} ({hrv.hrv_summary.status or '-'})",
        )
    label = "Activities"
    for activity in report.activities:
        start = activity.start_time_local
        clock = f"{start:%H:%M}  " if start is not None else ""
        details = (
            f"{_activity_type(activity)}  {_km(activity.distance)}  {_duration(activity.duration)}"
        )
        add(label, clock + details)
        label = ""
    if not report.activities:
        add(label, "none")
    return lines


for _name in dir(GarminClient):
    if (
        not _name.startswith("_")
        and _name not in _CUSTOM_COMMANDS
        and callable(getattr(GarminClient, _name))
    ):
        setattr(Cli, _name, _json_command(_name))


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``gcpapi`` command."""
    try:
        fire.Fire(Cli, command=argv, name="gcpapi")
    except (GarminError, FileExistsError, ValueError, TypeError) as exc:
        _note(f"error: {exc}")
        return 1
    return 0
