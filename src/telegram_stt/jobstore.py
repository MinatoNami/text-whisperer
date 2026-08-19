"""Crash-durable record of jobs that have been accepted but not finished.

The poll loop advances its update offset as soon as a message is *queued*, not
when it is transcribed — otherwise a long job would make the bot re-fetch the
same updates forever. That leaves a gap: a job sitting in the queue when the
process dies is lost, and Telegram will not resend it because the offset has
already moved past.

So every accepted job is written here before it is queued and removed only once
it reaches a terminal state. On startup anything still present is re-queued.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    @staticmethod
    def key(chat_id: int, message_id: int) -> str:
        return f"{chat_id}:{message_id}"

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # A corrupt file must not wedge startup; losing the pending list is
            # bad, refusing to boot is worse.
            log.warning("could not read %s (%s); starting with none", self.path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: never a half-written pending list

    def add(self, record: dict) -> None:
        with self._lock:
            data = self._read()
            data[self.key(record["chat_id"], record["message_id"])] = record
            self._write(data)

    def remove(self, chat_id: int, message_id: int) -> None:
        with self._lock:
            data = self._read()
            if data.pop(self.key(chat_id, message_id), None) is None:
                return
            self._write(data)

    def pending(self) -> list[dict]:
        with self._lock:
            return list(self._read().values())

    def count(self) -> int:
        with self._lock:
            return len(self._read())
