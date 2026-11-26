"""Atomic text updates with bounded, per-work recovery history."""

import re
import tempfile
import uuid
from pathlib import Path

MAX_VERSIONS = 5
VERSION_ID = re.compile(r"[0-9a-f]{32}")


def replace_work_text(path: Path, text: str):
    """Verify new content in the same directory before replacing the live file.

    Args:
        path: Existing, validated local draft path.
        text: Complete replacement, including the title line.

    Raises:
        OSError: The draft or a recovery copy could not be saved.
    """
    old = path.read_bytes()
    raw = text.encode("utf-8")
    if raw == old:
        return
    history = path.parent / ".versions" / path.stem
    history.mkdir(parents=True, exist_ok=True)
    if history.resolve() != history:
        raise OSError("Invalid draft history directory")
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(raw)
        if temp.read_bytes() != raw:
            raise OSError("Draft read-back verification failed")
        backup = history / (uuid.uuid4().hex + ".md")
        with backup.open("xb") as stream:
            stream.write(old)
        if backup.read_bytes() != old:
            raise OSError("Draft history read-back verification failed")
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    # A cleanup failure must not turn a committed update into a reported failure.
    try:
        for stale in list_work_versions(path)[MAX_VERSIONS:]:
            (history / (stale["id"] + ".md")).unlink(missing_ok=True)
    except OSError:
        pass


def list_work_versions(path: Path):
    """List recovery copies without accepting any client-provided file paths.

    Args:
        path: Current validated draft path.

    Returns:
        Version IDs and timestamps, newest first.
    """
    history = path.parent / ".versions" / path.stem
    if not history.is_dir() or history.resolve() != history:
        return []
    versions = [
        {"id": entry.stem, "saved_at": entry.stat().st_mtime}
        for entry in history.iterdir()
        if entry.suffix == ".md"
        and VERSION_ID.fullmatch(entry.stem)
        and entry.is_file()
        and not entry.is_symlink()
    ]
    return sorted(
        versions, key=lambda item: (item["saved_at"], item["id"]), reverse=True
    )
