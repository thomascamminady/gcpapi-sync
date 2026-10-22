# /// script
# requires-python = ">=3.11"
# dependencies = ["fire>=0.7"]
# ///
"""Compute and apply the next version: ``0.MINOR.YYYYMMDD``.

* ``MINOR`` counts releases with changes to this package. It is bumped at most once
  per release, no matter how many changes go into it.
* ``YYYYMMDD`` is the date on which the current state of Garmin's API was picked up.
  It changes only when Garmin changes something; our own changes keep it.

Examples, starting from the release 0.1.20261022::

    uv run scripts/bump_version.py feature --note "Add gcpapi plot"     # 0.2.20261022
    uv run scripts/bump_version.py garmin --note "New sleep fields"     # 0.1.20261029
    uv run scripts/bump_version.py show                                 # current/released

Both commands update ``pyproject.toml``, ``uv.lock`` and the top section of
``CHANGELOG.md``. Merging the result to ``main`` publishes the release.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import fire

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"
_VERSION = re.compile(r"^0\.(?P<minor>\d+)\.(?P<day>\d{8})$")
_SECTION = re.compile(r"^## \[(?P<version>[^\]]+)\] - (?P<date>\d{4}-\d{2}-\d{2})$", re.MULTILINE)


@dataclass(frozen=True, order=True)
class Version:
    """A ``0.MINOR.YYYYMMDD`` version."""

    minor: int
    day: date

    @classmethod
    def parse(cls, text: str) -> Version:
        """Parse ``0.MINOR.YYYYMMDD``."""
        match = _VERSION.match(text.strip())
        if match is None:
            raise SystemExit(f"{text!r} is not a 0.MINOR.YYYYMMDD version")
        day = datetime.strptime(match["day"], "%Y%m%d").replace(tzinfo=UTC).date()
        return cls(int(match["minor"]), day)

    @property
    def text(self) -> str:
        """The version string."""
        return f"0.{self.minor}.{self.day:%Y%m%d}"


def _executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SystemExit(f"{name} is not installed")
    return path


def current() -> Version:
    """The version in ``pyproject.toml``."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    return Version.parse(str(project["version"]))


def released() -> Version | None:
    """The newest version with a ``v...`` git tag, if any."""
    tags = subprocess.run(
        [_executable("git"), "tag", "--list", "v0.*"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    versions = [Version.parse(tag[1:]) for tag in tags if _VERSION.match(tag[1:])]
    return max(versions, default=None)


def _today() -> date:
    return datetime.now(UTC).date()


def _apply(new: Version, note: str) -> None:
    old = current()
    if new != old:
        # Drop the script's own environment so uv targets the project.
        env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
        command = [_executable("uv"), "version", new.text, "--no-sync"]
        subprocess.run(command, cwd=ROOT, check=True, env=env)
    _update_changelog(new, note)
    print(f"{old.text} -> {new.text}")


def _update_changelog(new: Version, note: str) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    top = _SECTION.search(text)
    last = released()
    heading = f"## [{new.text}] - {_today().isoformat()}"
    bullet = f"- {note}\n" if note else ""
    if top is not None and (last is None or Version.parse(top["version"]) > last):
        # The top section is not released yet: rename it and add the note.
        end = text.find("\n## [", top.end())
        end = len(text) if end < 0 else end
        section = text[top.end() : end].rstrip("\n")
        if bullet:
            section += ("\n" if section.strip() else "\n\n### Changed\n\n") + bullet.rstrip("\n")
        text = text[: top.start()] + heading + section + "\n" + text[end:]
    else:
        insert = top.start() if top is not None else len(text)
        body = f"\n\n### Changed\n\n{bullet}" if bullet else "\n"
        text = text[:insert] + heading + body + "\n" + text[insert:]
    CHANGELOG.write_text(text, encoding="utf-8")


class Bump:
    """Bump the version according to the 0.MINOR.YYYYMMDD scheme."""

    def show(self) -> None:
        """Print the current and the last released version."""
        last = released()
        print(f"current:  {current().text}")
        print(f"released: {last.text if last is not None else '-'}")

    def feature(self, note: str = "") -> None:
        """We changed the package: bump MINOR (once per release), keep the date.

        Args:
            note: Changelog entry.
        """
        version, last = current(), released()
        minor = version.minor if last is None or version.minor > last.minor else last.minor + 1
        _apply(Version(minor, version.day), note)

    def garmin(self, note: str = "", day: str = "") -> None:
        """Garmin changed its API: set the date, keep MINOR.

        Args:
            note: Changelog entry.
            day: The date to use (YYYYMMDD); today (UTC) by default.
        """
        version = current()
        when = Version.parse(f"0.0.{day}").day if day else _today()
        if when < version.day:
            raise SystemExit(f"{when} is before the current date component {version.day}")
        _apply(Version(version.minor, when), note)


if __name__ == "__main__":
    fire.Fire(Bump)
