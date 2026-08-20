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


def render_transcript(segments, *, with_timestamps: bool = True) -> str:
    """One line per Whisper segment, optionally prefixed with its position.

    Returns "" when there are no segments — Whisper occasionally hands back
    text with no segmentation, and the caller falls back to the flat string.
    """
    lines: list[str] = []
    for segment in segments:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        if with_timestamps:
            lines.append(f"[{timestamp(float(segment.get('start', 0.0)))}] {text}")
        else:
            lines.append(text)
    return "\n".join(lines)


def precise_duration(seconds: float) -> str:
    """Like human_duration, but keeps a decimal under 10s so a fast
    transcription doesn't report as '0s'."""
    if seconds < 10:
        return f"{seconds:.1f}s"
    return human_duration(seconds)


_LANGUAGE_NAMES = {
    "en": "English", "zh": "Chinese", "ms": "Malay", "ta": "Tamil", "id": "Indonesian",
    "ja": "Japanese", "ko": "Korean", "th": "Thai", "vi": "Vietnamese", "hi": "Hindi",
    "fr": "French", "de": "German", "es": "Spanish", "it": "Italian", "pt": "Portuguese",
    "nl": "Dutch", "ru": "Russian", "ar": "Arabic", "tr": "Turkish", "pl": "Polish",
}


def language_name(code: str | None) -> str | None:
    if not code:
        return None
    return _LANGUAGE_NAMES.get(code.lower(), code.upper())


def spoken_duration(seconds: float | None) -> str:
    """Duration the way a person would say it: '51 min', not '51m17s'."""
    if not seconds:
        return ""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} sec"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} hr" if not rest else f"{hours} hr {rest} min"


def eta(fraction: float, elapsed: float) -> str:
    """A rough '20 sec left', or '' when there is nothing useful to say yet."""
    if fraction <= 0.05 or fraction >= 0.99 or elapsed <= 0:
        return ""
    remaining = elapsed / fraction - elapsed
    if remaining < 3:
        return ""
    if remaining < 60:
        return f"about {int(round(remaining / 5) * 5)} sec left"
    return f"about {int(round(remaining / 60))} min left"


def escape_html(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def summary_to_html(markdown: str) -> str:
    """Render a Markdown summary as the small HTML subset Telegram accepts.

    Telegram has no headings or list syntax, so sections become bold lines and
    bullets become real bullet characters.
    """
    out: list[str] = []
    for raw in (markdown or "").splitlines():
        line = raw.strip()
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        if line.startswith("#"):
            heading = escape_html(line.lstrip("#").strip())
            if out and out[-1] != "":
                out.append("")
            out.append(f"<b>{heading}</b>")
            continue
        if line[0] in "-*•" and (len(line) > 1 and line[1] == " "):
            body = _inline_html(line[2:].strip())
            out.append(f"• {body}")
            continue
        out.append(_inline_html(line))
    return "\n".join(out).strip()


def _inline_html(text: str) -> str:
    """Escape, then re-apply **bold** as <b>."""
    escaped = escape_html(text)
    parts = escaped.split("**")
    if len(parts) < 3:
        return escaped
    rebuilt = ""
    for index, part in enumerate(parts):
        rebuilt += f"<b>{part}</b>" if index % 2 else part
    return rebuilt


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
