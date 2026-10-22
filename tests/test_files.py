from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from gcpapi_sync import ActivityFile
from gcpapi_sync._files import extract_activity_file
from tests.helpers import zip_archive

if TYPE_CHECKING:
    from pathlib import Path


def test_extracts_the_only_file() -> None:
    result = extract_activity_file(1, zip_archive({"1_ACTIVITY.gpx": b"<gpx/>"}))

    assert result == ActivityFile(1, "1_ACTIVITY.gpx", b"<gpx/>")
    assert result.extension == "gpx"
    assert "gpx/>" not in repr(result)


def test_prefers_the_fit_file() -> None:
    archive = zip_archive({"notes.txt": b"x", "nested/1_ACTIVITY.FIT": b"fit"})

    result = extract_activity_file(1, archive)

    assert result.filename == "1_ACTIVITY.FIT"
    assert result.extension == "fit"


@pytest.mark.parametrize("name", ["../../etc/evil.fit", "/abs/evil.fit", "..\\..\\evil.fit"])
def test_member_paths_are_reduced_to_base_names(name: str) -> None:
    assert extract_activity_file(1, zip_archive({name: b"x"})).filename == "evil.fit"


@pytest.mark.parametrize(
    ("archive", "message"),
    [(b"not a zip", "not a zip"), (zip_archive({}), "empty")],
)
def test_invalid_archives(archive: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        extract_activity_file(1, archive)


def test_oversized_members_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gcpapi_sync._files.MAX_ACTIVITY_FILE_SIZE", 3)

    with pytest.raises(ValueError, match="implausibly large"):
        extract_activity_file(1, zip_archive({"1.fit": b"1234"}))


def test_save_into_directory_and_to_path(tmp_path: Path) -> None:
    tmp_path /= "out"
    tmp_path.mkdir()
    activity = ActivityFile(1, "1_ACTIVITY.fit", b"data")

    saved = activity.save(tmp_path)
    assert saved == tmp_path / "1_ACTIVITY.fit"
    assert saved.read_bytes() == b"data"

    with pytest.raises(FileExistsError):
        activity.save(tmp_path)
    ActivityFile(1, "1_ACTIVITY.fit", b"new").save(tmp_path, overwrite=True)
    assert saved.read_bytes() == b"new"

    custom = activity.save(tmp_path / "sub" / "run.fit")
    assert custom.read_bytes() == b"data"
    assert sorted(path.name for path in tmp_path.rglob("*")) == ["1_ACTIVITY.fit", "run.fit", "sub"]


def test_save_cleans_up_after_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.replace", fail)
    target = tmp_path / "out"
    target.mkdir()

    with pytest.raises(OSError, match="disk full"):
        ActivityFile(1, "a.fit", b"x").save(target)
    assert list(target.iterdir()) == []
