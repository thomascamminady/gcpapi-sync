"""Activity file downloads."""

from __future__ import annotations

import io
import os
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePath

from ._constants import MAX_ACTIVITY_FILE_SIZE

__all__ = ["ActivityFile"]


@dataclass(frozen=True, slots=True)
class ActivityFile:
    """An activity in its original upload format (usually FIT), as recorded by the device.

    Attributes:
        activity_id: The activity the file belongs to.
        filename: File name inside Garmin's archive, such as ``12345_ACTIVITY.fit``.
        content: The file's bytes.
    """

    activity_id: int
    filename: str
    content: bytes = field(repr=False)

    @property
    def extension(self) -> str:
        """Lower-case file extension without the dot, such as ``"fit"``, ``"gpx"`` or ``"tcx"``."""
        return PurePath(self.filename).suffix.lstrip(".").lower()

    def save(self, path: str | os.PathLike[str] = ".", *, overwrite: bool = False) -> Path:
        """Write the file and return its path.

        Args:
            path: A directory (the original file name is kept) or a file path.
            overwrite: Replace an existing file instead of raising ``FileExistsError``.
        """
        target = Path(path).expanduser()
        if target.is_dir():
            target /= self.filename
        if target.exists() and not overwrite:
            raise FileExistsError(f"{target} already exists; pass overwrite=True to replace it")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(self.content)
            Path(temporary).replace(target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return target


def extract_activity_file(activity_id: int, archive: bytes) -> ActivityFile:
    """Pick the activity file out of Garmin's download archive.

    Raises:
        ValueError: The archive is not a zip file, is empty, or holds an oversized file.
    """
    try:
        bundle = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as exc:
        raise ValueError("the download is not a zip archive") from exc
    with bundle:
        members = [info for info in bundle.infolist() if not info.is_dir()]
        if not members:
            raise ValueError("the download archive is empty")
        # Prefer the FIT file; Garmin archives normally contain exactly one file.
        member = next((m for m in members if m.filename.lower().endswith(".fit")), members[0])
        if member.file_size > MAX_ACTIVITY_FILE_SIZE:
            raise ValueError(f"{member.filename} is implausibly large ({member.file_size} bytes)")
        # Only keep the base name; archive member names are untrusted paths.
        name = PurePath(member.filename.replace("\\", "/")).name or f"{activity_id}.fit"
        return ActivityFile(activity_id=activity_id, filename=name, content=bundle.read(member))
