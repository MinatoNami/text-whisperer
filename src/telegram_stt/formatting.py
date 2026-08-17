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


def timestamp(seconds: float) -> str:
    """Clock position within a recording, as [MM:SS] or [H:MM:SS]."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def progress_bar(fraction: float, width: int = 12) -> str:
    fraction = min(1.0, max(0.0, fraction))
    # Floor rather than round, so the bar only reads full at an actual 100%.
    filled = width if fraction >= 1.0 else int(fraction * width)
    return f"{'█' * filled}{'░' * (width - filled)} {fraction * 100:.0f}%"


def speaker_name(index: int | None) -> str:
    return "Unknown" if index is None else f"Speaker {index + 1}"


def render_transcript(
    segments,
    speaker_labels=None,
    *,
    with_timestamps: bool = True,
) -> str:
    """Lay out the transcript, grouping consecutive lines from one speaker.

    Falls back to a plain paragraph when there are no segments, which happens
    if Whisper returns text without segmentation.
    """
    segments = list(segments)
    if not segments:
        return ""

    labels = list(speaker_labels) if speaker_labels else [None] * len(segments)
    lines: list[str] = []
    current_speaker: object = object()  # sentinel: never equal to a real label
    buffer: list[str] = []
    buffer_start = 0.0

    def flush() -> None:
        if not buffer:
            return
        prefix = ""
        if with_timestamps:
            prefix += f"[{timestamp(buffer_start)}] "
        if current_speaker is not None:
            prefix += f"{speaker_name(current_speaker)}: "
        lines.append(prefix + " ".join(buffer))

    for segment, label in zip(segments, labels):
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        if label != current_speaker:
            flush()
            buffer = []
            current_speaker = label
            buffer_start = float(segment.get("start", 0.0))
        buffer.append(text)
    flush()
    return "\n".join(lines)


def precise_duration(seconds: float) -> str:
    """Like human_duration, but keeps a decimal under 10s so a fast
    transcription doesn't report as '0s'."""
    if seconds < 10:
        return f"{seconds:.1f}s"
    return human_duration(seconds)


def footer(
    model: str,
    language: str | None,
    audio_seconds: float,
    elapsed: float,
    speaker_count: int = 0,
) -> str:
    model_name = model.rsplit("/", 1)[-1]
    speed = audio_seconds / elapsed if elapsed > 0 else 0.0
    parts = [model_name]
    if language:
        parts.append(language)
    if speaker_count:
        parts.append(f"{speaker_count} speaker{'s' if speaker_count != 1 else ''}")
    parts.append(
        f"{human_duration(audio_seconds)} audio in {precise_duration(elapsed)} ({speed:.1f}×)"
    )
    return "— " + " · ".join(parts)
