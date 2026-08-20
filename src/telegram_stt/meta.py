"""Editable metadata for an archived recording.

history.jsonl is append-only: it records what happened, survives a crash
mid-write, and greps cleanly. Titles, tags and deletions are *mutations*, so
they do not belong in it. Each recording gets a sidecar `<stem>.meta.json`
holding what you decided about it, which keeps the index immutable and keeps
the whole archive a directory of files that rsync can back up.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_LOCK = threading.Lock()


@dataclass
class RecordMeta:
    title: str = ""
    tags: list[str] = field(default_factory=list)
    note: str = ""
    deleted: bool = False
    deleted_at: str | None = None
    # Set once the audio has been pruned, so the UI can say why it is missing
    # rather than looking broken.
    audio_pruned: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "RecordMeta":
        return RecordMeta(
            title=str(data.get("title") or ""),
            tags=[str(t) for t in (data.get("tags") or []) if str(t).strip()],
            note=str(data.get("note") or ""),
            deleted=bool(data.get("deleted")),
            deleted_at=data.get("deleted_at"),
            audio_pruned=bool(data.get("audio_pruned")),
        )


def path_for(archive_root: Path, text_file: str | None) -> Path | None:
    """Where a record's sidecar lives, given its transcript path."""
    if not text_file:
        return None
    candidate = (archive_root / text_file).resolve()
    try:
        candidate.relative_to(archive_root.resolve())
    except ValueError:
        log.warning("refusing a metadata path outside the archive: %s", text_file)
        return None
    return candidate.with_suffix(".meta.json")


def read(archive_root: Path, text_file: str | None) -> RecordMeta:
    path = path_for(archive_root, text_file)
    if path is None or not path.is_file():
        return RecordMeta()
    try:
        return RecordMeta.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        # Bad metadata must not hide a recording; fall back to none of it.
        log.warning("ignoring unreadable metadata %s: %s", path, exc)
        return RecordMeta()


def write(archive_root: Path, text_file: str | None, meta: RecordMeta) -> Path | None:
    path = path_for(archive_root, text_file)
    if path is None:
        return None
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta.to_dict(), ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, path)   # never leave a half-written sidecar
    return path


def update(archive_root: Path, text_file: str | None, **changes) -> RecordMeta:
    """Read-modify-write one recording's metadata."""
    meta = read(archive_root, text_file)
    for key, value in changes.items():
        if not hasattr(meta, key):
            raise KeyError(f"unknown metadata field {key!r}")
        setattr(meta, key, value)
    if changes.get("deleted") and not meta.deleted_at:
        meta.deleted_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    if changes.get("deleted") is False:
        meta.deleted_at = None
    write(archive_root, text_file, meta)
    return meta
