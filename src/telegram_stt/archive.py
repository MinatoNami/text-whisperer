"""On-disk archive of every audio file received and transcript produced.

Layout under the archive root:

    2026-08-17/
      20260817-181233-<chat>-<message>.ogg    original audio, as received
      20260817-181233-<chat>-<message>.txt    rendered transcript
      20260817-181233-<chat>-<message>.json   per-segment text + timestamps
    history.jsonl                             one line per job, newest last

The JSONL index is append-only so it survives crashes mid-write and can be
tailed or grepped without parsing the whole archive.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

INDEX_NAME = "history.jsonl"


@dataclass
class ArchiveEntry:
    stem: str
    day: str
    audio_path: Path | None = None
    text_path: Path | None = None
    meta_path: Path | None = None
    record: dict = field(default_factory=dict)


class Archive:
    """Writes audio + transcripts to disk and maintains the history index."""

    def __init__(self, root: Path, *, keep_audio: bool = True):
        self.root = root
        self.keep_audio = keep_audio
        self._lock = threading.Lock()

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_NAME

    def _slot(self, chat_id: int, message_id: int, when: datetime) -> ArchiveEntry:
        day = when.strftime("%Y-%m-%d")
        stem = f"{when.strftime('%Y%m%d-%H%M%S')}-{chat_id}-{message_id}"
        (self.root / day).mkdir(parents=True, exist_ok=True)
        return ArchiveEntry(stem=stem, day=day)

    def store(
        self,
        *,
        chat_id: int,
        message_id: int,
        source_audio: Path,
        transcript_text: str,
        media_kind: str,
        original_name: str | None,
        language: str | None,
        model: str,
        audio_seconds: float,
        elapsed_seconds: float,
        segments,
    ) -> ArchiveEntry:
        """Copy the audio in, write the transcript, append to the index."""
        when = datetime.now(timezone.utc).astimezone()
        entry = self._slot(chat_id, message_id, when)
        day_dir = self.root / entry.day

        if self.keep_audio and source_audio.is_file():
            suffix = source_audio.suffix or Path(original_name or "").suffix or ".bin"
            entry.audio_path = day_dir / f"{entry.stem}{suffix}"
            try:
                # copy, not move: the Bot API server's copy is cleaned up
                # separately and may still be needed if this throws.
                shutil.copy2(source_audio, entry.audio_path)
            except OSError as exc:
                log.warning("could not archive audio %s: %s", source_audio, exc)
                entry.audio_path = None

        entry.text_path = day_dir / f"{entry.stem}.txt"
        entry.text_path.write_text(transcript_text, encoding="utf-8")

        entry.meta_path = day_dir / f"{entry.stem}.json"
        detail = {
            "segments": [
                {
                    "start": round(float(s.get("start", 0.0)), 3),
                    "end": round(float(s.get("end", 0.0)), 3),
                    "text": (s.get("text") or "").strip(),
                }
                for s in segments
            ]
        }
        entry.meta_path.write_text(json.dumps(detail, ensure_ascii=False, indent=2), "utf-8")

        entry.record = {
            "timestamp": when.isoformat(timespec="seconds"),
            "chat_id": chat_id,
            "message_id": message_id,
            "media_kind": media_kind,
            "original_name": original_name,
            "language": language,
            "model": model,
            "audio_seconds": round(audio_seconds, 2),
            "elapsed_seconds": round(elapsed_seconds, 2),
            "characters": len(transcript_text),
            "audio_file": str(entry.audio_path.relative_to(self.root)) if entry.audio_path else None,
            "text_file": str(entry.text_path.relative_to(self.root)),
            "meta_file": str(entry.meta_path.relative_to(self.root)),
        }
        self._append(entry.record)
        log.info("archived %s (%s)", entry.stem, entry.text_path)
        return entry

    def _append(self, record: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            with self.index_path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def stats(self) -> dict:
        """Totals for /history, read straight off the index."""
        count = 0
        audio_seconds = 0.0
        characters = 0
        last: dict | None = None
        if not self.index_path.is_file():
            return {"count": 0, "audio_seconds": 0.0, "characters": 0, "last": None}
        with self.index_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # a torn final line should not break the summary
                count += 1
                audio_seconds += float(record.get("audio_seconds") or 0)
                characters += int(record.get("characters") or 0)
                last = record
        return {
            "count": count,
            "audio_seconds": audio_seconds,
            "characters": characters,
            "last": last,
        }

    def recent(self, limit: int = 5) -> list[dict]:
        if not self.index_path.is_file():
            return []
        records: list[dict] = []
        with self.index_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
        return records[-limit:][::-1]

    def disk_usage(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
