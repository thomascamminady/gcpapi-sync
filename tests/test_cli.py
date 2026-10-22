from __future__ import annotations

import io
import json
import os
import runpy
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import JsonValue, TypeAdapter

from gcpapi_sync import GarminClient, TokenCache, cli
from gcpapi_sync._compat import override
from tests.cases import ACTIVITY_ID, ZIP
from tests.helpers import API_KEY, BASE_URL, NO_WAIT, FakeGarmin, Reply, zip_archive

if TYPE_CHECKING:
    from pathlib import Path

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
SEARCH = "/activitylist-service/activities/search/activities"
DOWNLOAD = "/download-service/files/activity/"


@pytest.fixture(autouse=True)
def fake_api(monkeypatch: pytest.MonkeyPatch, fake: FakeGarmin) -> FakeGarmin:
    """Point every GarminClient the CLI creates at the fake API."""

    def client(
        api_key: str | None = None,
        *,
        key_file: str | None = None,
        base_url: str | None = None,
        token_cache: TokenCache | None = None,
    ) -> GarminClient:
        return GarminClient(
            api_key,
            key_file=key_file,
            base_url=base_url or BASE_URL,
            token_cache=token_cache,
            retry=NO_WAIT,
            http_client=httpx.Client(transport=fake.transport()),
        )

    monkeypatch.setattr(cli, "GarminClient", client)
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", API_KEY)
    return fake


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> JsonValue:
    code, out, err = run(capsys, *argv)
    assert code == 0, err
    return _JSON.validate_json(out)


def activity_page(*ids: int) -> Reply:
    return Reply.json(
        [
            {
                "activityId": activity_id,
                "activityName": f"Run {activity_id}",
                "startTimeLocal": "2026-10-21 08:23:10",
                "activityType": {"typeKey": "running"},
                "distance": 10123.4,
                "duration": 3125.0,
                "averageHR": 151.0,
            }
            for activity_id in ids
        ]
    )


def test_json_command_for_every_client_method(
    capsys: pytest.CaptureFixture[str], fake: FakeGarmin
) -> None:
    fake.reply(
        "/usersummary-service/stats/steps/daily/2026-10-19/2026-10-21", Reply.fixture("daily_steps")
    )

    data = run_json(capsys, "get_daily_steps", "2026-10-19", "2026-10-21", "--indent", "0")

    assert isinstance(data, list)
    assert isinstance(data[0], dict)
    assert "totalSteps" in data[0]
    assert all(value is not None for value in data[0].values())
    # Every client method is a command; get_access_token is `token`, which hides the value.
    for name in dir(GarminClient):
        if not name.startswith("_") and callable(getattr(GarminClient, name)):
            command = {"close": None, "get_access_token": "token"}.get(name, name)
            assert command is None or callable(getattr(cli.Cli, command)), name


def test_token_never_prints_the_token(capsys: pytest.CaptureFixture[str], fake: FakeGarmin) -> None:
    data = run_json(capsys, "token", "--no_cache")

    assert isinstance(data, dict)
    assert set(data) == {"expiresAt"}
    assert fake.issued[0] not in json.dumps(data)


def test_token_cache_is_used_by_default(
    capsys: pytest.CaptureFixture[str], fake: FakeGarmin, isolated_environment: Path
) -> None:
    run_json(capsys, "token")
    run_json(capsys, "token")

    assert len(fake.token_requests) == 1
    assert (isolated_environment / ".config/garmin/connect-personal-api-bearer-token.json").exists()


def test_errors_exit_with_status_1(capsys: pytest.CaptureFixture[str], fake: FakeGarmin) -> None:
    fake.reply(f"/activity-service/activity/{ACTIVITY_ID}", Reply(status=404))

    code, out, err = run(capsys, "get_activity", str(ACTIVITY_ID))

    assert code == 1
    assert out == ""
    assert "error: GET /activity-service/activity" in err


def test_today(capsys: pytest.CaptureFixture[str], fake: FakeGarmin) -> None:
    fake.reply("/usersummary-service/usersummary/daily", Reply.fixture("daily_summary"))
    fake.reply("/sleep-service/sleep/dailySleep", Reply.fixture("daily_sleep"))
    fake.reply("/hrv-service/hrv/2026-10-21", Reply.fixture("daily_hrv"))
    fake.reply(SEARCH, activity_page(1, 2))

    code, out, _ = run(capsys, "today", "2026-10-21")

    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "Wednesday, 2026-10-21"
    labels = [line.split()[0] for line in lines[1:] if not line.startswith("                ")]
    assert labels == [
        "Steps",
        "Distance",
        "Resting",
        "Stress",
        "Body",
        "Sleep",
        "HRV",
        "Activities",
    ]
    assert "Activities    08:23  running  10.12 km  52:05" in out


def test_today_without_data(capsys: pytest.CaptureFixture[str], fake: FakeGarmin) -> None:
    fake.reply("/usersummary-service/usersummary/daily", Reply.json({}))
    fake.reply("/sleep-service/sleep/dailySleep", Reply.json({}))
    fake.reply("/hrv-service/hrv/2026-10-21", Reply(status=204))
    fake.reply(SEARCH, Reply.json([]))

    code, out, _ = run(capsys, "today", "2026-10-21")

    assert code == 0
    assert "Steps         - of -" in out
    assert "Activities    none" in out
    assert "HRV" not in out


def test_today_as_json(capsys: pytest.CaptureFixture[str], fake: FakeGarmin) -> None:
    fake.reply("/usersummary-service/usersummary/daily", Reply.fixture("daily_summary"))
    fake.reply("/sleep-service/sleep/dailySleep", Reply.fixture("daily_sleep"))
    fake.reply("/hrv-service/hrv/2026-10-21", Reply(status=204))
    fake.reply(SEARCH, activity_page(1))

    data = run_json(capsys, "today", "2026-10-21", "--as_json")

    assert isinstance(data, dict)
    assert set(data) == {"day", "summary", "sleep", "activities"}
    assert data["day"] == "2026-10-21"


def test_activities_table(capsys: pytest.CaptureFixture[str], fake: FakeGarmin) -> None:
    fake.reply(SEARCH, activity_page(11, 12))

    code, out, _ = run(capsys, "activities", "--limit", "2", "--activity_type", "running")

    assert code == 0
    header, first, second = out.splitlines()
    assert header.split() == ["Start", "Type", "Name", "Distance", "Duration", "Avg", "HR", "ID"]
    assert first.split()[-1] == "11"
    assert second.split()[-1] == "12"
    assert "10.12 km" in first
    assert "52:05" in first
    assert fake.data_requests[0].url.params["activityType"] == "running"


def test_sync_downloads_new_activities_only(
    capsys: pytest.CaptureFixture[str], fake: FakeGarmin, tmp_path: Path
) -> None:
    fake.reply(SEARCH, activity_page(1, 2, 3))

    def download(request: httpx.Request) -> Reply:
        activity_id = request.url.path.rsplit("/", 1)[1]
        if activity_id == "3":
            return Reply(status=404)
        return Reply(body=zip_archive({f"{activity_id}_ACTIVITY.fit": b"fit"}))

    fake.respond_with(DOWNLOAD, download)
    target = tmp_path / "fit"
    target.mkdir()
    (target / "2_ACTIVITY.fit").write_bytes(b"already there")

    first = run_json(capsys, "sync", str(target), "--metadata", "--since", "2026-10-01")
    second = run_json(capsys, "sync", str(target))

    assert first == {
        "directory": str(target),
        "downloaded": ["1_ACTIVITY.fit"],
        "skipped": 1,
        "unavailable": [3],
    }
    assert sorted(path.name for path in target.iterdir()) == [
        "1.json",
        "1_ACTIVITY.fit",
        "2_ACTIVITY.fit",
    ]
    assert fake.data_requests[0].url.params["startDate"] == "2026-10-01"
    assert isinstance(second, dict)
    assert second["downloaded"] == []
    assert second["skipped"] == 2
    assert second["unavailable"] == [3]


def test_sync_respects_limit(
    capsys: pytest.CaptureFixture[str], fake: FakeGarmin, tmp_path: Path
) -> None:
    fake.reply(SEARCH, activity_page(1, 2), Reply.json([]))
    fake.respond_with(DOWNLOAD, lambda _request: Reply(body=ZIP))
    (tmp_path / "2_ACTIVITY.fit").write_bytes(b"old")

    report = run_json(capsys, "sync", str(tmp_path), "--limit", "1", "--until", "2026-10-21")

    assert isinstance(report, dict)
    assert report["skipped"] == 0
    assert len(fake.requests) == 3
    assert fake.data_requests[0].url.params["endDate"] == "2026-10-21"


def test_activities_without_ids_are_rejected(
    capsys: pytest.CaptureFixture[str], fake: FakeGarmin, tmp_path: Path
) -> None:
    fake.reply(SEARCH, Reply.json([{"activityName": "no id"}]))

    code, _, err = run(capsys, "sync", str(tmp_path / "out"))

    assert code == 1
    assert "activityId" in err


def test_export(capsys: pytest.CaptureFixture[str], fake: FakeGarmin, tmp_path: Path) -> None:
    def empty(request: httpx.Request) -> Reply:
        if "dailysummaries" in request.url.path:
            return Reply.json({"data": []})
        if request.url.path.startswith("/hrv-service"):
            return Reply.json({"hrvSummaries": []})
        return Reply.json([])

    fake.respond_with("/", empty)

    report = run_json(capsys, "export", "2026-10-01", "2026-10-02", str(tmp_path))

    assert isinstance(report, dict)
    records = report["records"]
    assert isinstance(records, dict)
    assert sorted(records) == sorted(path.stem for path in tmp_path.glob("*.json"))
    assert len(records) == 8


def test_download_commands(
    capsys: pytest.CaptureFixture[str], fake: FakeGarmin, tmp_path: Path
) -> None:
    fake.reply(f"{DOWNLOAD}{ACTIVITY_ID}", Reply(body=ZIP))

    saved = run_json(capsys, "download_activity", str(ACTIVITY_ID), "--output", str(tmp_path))
    archive = run_json(capsys, "download_activity_zip", str(ACTIVITY_ID), "--output", str(tmp_path))
    code, _, err = run(capsys, "download_activity_zip", str(ACTIVITY_ID), "--output", str(tmp_path))
    custom = run_json(
        capsys, "download_activity_zip", str(ACTIVITY_ID), "--output", str(tmp_path / "a.zip")
    )

    assert saved == {"path": str(tmp_path / f"{ACTIVITY_ID}_ACTIVITY.fit")}
    assert archive == {"path": str(tmp_path / f"{ACTIVITY_ID}.zip")}
    assert (tmp_path / f"{ACTIVITY_ID}.zip").read_bytes() == ZIP
    assert code == 1
    assert "already exists" in err
    assert custom == {"path": str(tmp_path / "a.zip")}


def test_iter_activities_prints_json_lines(
    capsys: pytest.CaptureFixture[str], fake: FakeGarmin
) -> None:
    fake.reply(SEARCH, activity_page(1, 2), Reply.json([]))

    code, out, _ = run(capsys, "iter_activities", "--page_size", "2")

    assert code == 0
    lines = [_JSON.validate_json(line) for line in out.splitlines()]
    assert [line["activityId"] for line in lines if isinstance(line, dict)] == [1, 2]


def test_setup_verifies_and_stores_the_key(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, isolated_environment: Path
) -> None:
    monkeypatch.delenv("GARMIN_PERSONAL_API_KEY")
    monkeypatch.setattr("sys.stdin", io.StringIO(API_KEY + "\n"))
    target = isolated_environment / ".config/garmin/connect-personal-api-key"

    code, out, _ = run(capsys, "setup", "--no_cache")

    assert code == 0
    assert "Key verified and saved" in out
    assert target.read_text() == API_KEY + "\n"
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600
    code, _, err = run(capsys, "setup")
    assert code == 1
    assert "already exists" in err


def test_setup_reads_from_a_hidden_prompt_on_terminals(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Terminal(io.StringIO):
        @override
        def isatty(self) -> bool:
            return True

    prompts: list[str] = []

    def getpass(prompt: str) -> str:
        prompts.append(prompt)
        return API_KEY

    monkeypatch.setattr("sys.stdin", Terminal())
    monkeypatch.setattr("getpass.getpass", getpass)

    code, _, _ = run(capsys, "setup", "--overwrite")

    assert code == 0
    assert prompts == ["Garmin Personal API key (input hidden): "]


def test_setup_rejects_invalid_keys(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, isolated_environment: Path
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("wrong-key\n"))

    code, _, err = run(capsys, "setup")

    assert code == 1
    assert "rejected the API key" in err
    assert not (isolated_environment / ".config/garmin/connect-personal-api-key").exists()


def test_status(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "status")

    assert code == 0
    assert "API key       environment variable GARMIN_PERSONAL_API_KEY" in out
    assert "Access token  valid until" in out
    assert "left)" in out


def test_status_warns_about_shared_key_files(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if os.name != "posix":
        pytest.skip("POSIX file permissions")
    key_file = tmp_path / "key"
    key_file.write_text(API_KEY)
    key_file.chmod(0o644)
    monkeypatch.delenv("GARMIN_PERSONAL_API_KEY")

    code, out, _ = run(capsys, "status", "--key_file", str(key_file), "--no_cache")

    assert code == 0
    assert "WARNING: readable by others" in out
    assert "Token cache   disabled" in out


def test_logout(capsys: pytest.CaptureFixture[str], isolated_environment: Path) -> None:
    config = isolated_environment / ".config/garmin"
    run_json(capsys, "token")
    (config / "connect-personal-api-key").write_text("k")

    code, _, _ = run(capsys, "logout")
    assert code == 0
    assert not (config / "connect-personal-api-bearer-token.json").exists()
    assert (config / "connect-personal-api-key").exists()

    run(capsys, "logout", "--forget_key")
    assert not (config / "connect-personal-api-key").exists()


def test_today_defaults_to_the_current_date(
    capsys: pytest.CaptureFixture[str], fake: FakeGarmin
) -> None:
    fake.reply("/usersummary-service/usersummary/daily", Reply.json({}))
    fake.reply("/sleep-service/sleep/dailySleep", Reply.json({}))
    fake.respond_with("/hrv-service/hrv/", lambda _request: Reply(status=204))
    fake.reply(SEARCH, Reply.json([]))

    code, _, _ = run(capsys, "today")

    assert code == 0
    requested = fake.data_requests[0].url.params["calendarDate"]
    assert requested == cli._today().isoformat()


def test_sync_skips_files_that_exist_under_another_name(
    capsys: pytest.CaptureFixture[str], fake: FakeGarmin, tmp_path: Path
) -> None:
    fake.reply(SEARCH, activity_page(5))
    fake.reply(f"{DOWNLOAD}5", Reply(body=zip_archive({"renamed.fit": b"fit"})))
    (tmp_path / "renamed.fit").write_bytes(b"old")

    report = run_json(capsys, "sync", str(tmp_path))

    assert isinstance(report, dict)
    assert report["skipped"] == 1
    assert (tmp_path / "renamed.fit").read_bytes() == b"old"


def test_python_dash_m_runs_the_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["gcpapi", "token", "--no_cache"])

    with pytest.raises(SystemExit) as exited:
        runpy.run_module("gcpapi_sync", run_name="__main__")

    assert exited.value.code == 0
    assert "expiresAt" in capsys.readouterr().out
