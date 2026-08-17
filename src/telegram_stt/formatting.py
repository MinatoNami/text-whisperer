"""Turning a transcript into something Telegram will actually accept."""

from __future__ import annotations


def human_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return "unknown size"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def human_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def precise_duration(seconds: float) -> str:
    """Like human_duration, but keeps a decimal under 10s so a fast
    transcription doesn't report as '0s'."""
    if seconds < 10:
        return f"{seconds:.1f}s"
    return human_duration(seconds)


def footer(model: str, language: str | None, audio_seconds: float, elapsed: float) -> str:
    model_name = model.rsplit("/", 1)[-1]
    speed = audio_seconds / elapsed if elapsed > 0 else 0.0
    parts = [model_name]
    if language:
        parts.append(language)
    parts.append(
        f"{human_duration(audio_seconds)} audio in {precise_duration(elapsed)} ({speed:.1f}×)"
    )
    return "— " + " · ".join(parts)
