"""Turning a transcript into something Telegram will actually accept."""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+")


def human_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _split_oversized(piece: str, limit: int) -> list[str]:
    """Last resort for a run of text with no break points — split on spaces,
    then mid-word if even that fails."""
    out: list[str] = []
    while len(piece) > limit:
        cut = piece.rfind(" ", 0, limit)
        if cut <= limit // 2:
            cut = limit
        out.append(piece[:cut].strip())
        piece = piece[cut:].lstrip()
    if piece:
        out.append(piece)
    return out


def chunk(text: str, limit: int) -> list[str]:
    """Split text into <=limit pieces, preferring paragraph then sentence
    boundaries so a transcript never breaks mid-thought if avoidable."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    units: list[str] = []
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= limit:
            units.append(paragraph)
            continue
        for sentence in _SENTENCE_END.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= limit:
                units.append(sentence)
            else:
                units.extend(_split_oversized(sentence, limit))

    chunks: list[str] = []
    current = ""
    for unit in units:
        if not current:
            current = unit
        elif len(current) + 1 + len(unit) <= limit:
            current = f"{current} {unit}"
        else:
            chunks.append(current)
            current = unit
    if current:
        chunks.append(current)
    return chunks


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
