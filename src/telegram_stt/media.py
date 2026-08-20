"""Recognising which incoming messages carry audio worth transcribing."""

from __future__ import annotations

from dataclasses import dataclass

# Checked in order; `audio` before `document` so a properly tagged music file
# wins over the generic attachment path.
_MEDIA_FIELDS = ("voice", "audio", "video_note", "video", "document")

_AUDIO_EXTENSIONS = {
    ".aac", ".aiff", ".amr", ".caf", ".flac", ".m4a", ".m4b", ".mka", ".mp3",
    ".mp4", ".mov", ".mpeg", ".mpga", ".oga", ".ogg", ".opus", ".wav", ".webm",
    ".wma", ".mkv",
}

_KIND_LABELS = {
    "voice": "voice note",
    "audio": "audio file",
    "video_note": "video note",
    "video": "video",
    "document": "file",
}


@dataclass(frozen=True)
class Media:
    file_id: str
    kind: str
    duration: int | None
    file_name: str | None
    file_size: int | None
    # file_id is per-bot and can change; file_unique_id is stable for the same
    # content, which is what makes recognising a re-send possible. Last, with a
    # default, so positional construction keeps working.
    file_unique_id: str | None = None

    @property
    def label(self) -> str:
        return _KIND_LABELS.get(self.kind, self.kind)

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "file_unique_id": self.file_unique_id,
            "kind": self.kind,
            "duration": self.duration,
            "file_name": self.file_name,
            "file_size": self.file_size,
        }

    @staticmethod
    def from_dict(data: dict) -> "Media":
        return Media(
            file_id=data["file_id"],
            file_unique_id=data.get("file_unique_id"),
            kind=data.get("kind") or "voice",
            duration=data.get("duration"),
            file_name=data.get("file_name"),
            file_size=data.get("file_size"),
        )


def _looks_like_audio(payload: dict) -> bool:
    mime = (payload.get("mime_type") or "").lower()
    if mime.startswith(("audio/", "video/")):
        return True
    name = (payload.get("file_name") or "").lower()
    return any(name.endswith(ext) for ext in _AUDIO_EXTENSIONS)


def extract_media(message: dict) -> Media | None:
    """Return the transcribable attachment on a message, if there is one."""
    for field in _MEDIA_FIELDS:
        payload = message.get(field)
        if not isinstance(payload, dict) or not payload.get("file_id"):
            continue
        if field in ("document", "video") and not _looks_like_audio(payload):
            continue
        return Media(
            file_id=payload["file_id"],
            kind=field,
            duration=payload.get("duration"),
            file_name=payload.get("file_name"),
            file_size=payload.get("file_size"),
            file_unique_id=payload.get("file_unique_id"),
        )
    return None
