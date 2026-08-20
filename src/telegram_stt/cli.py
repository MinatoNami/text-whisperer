"""Run a local audio file through the transcription pipeline.

The same decode -> transcribe -> render -> archive path the bot uses, minus
Telegram. This is the safe way to trigger a job while developing: it needs no
bot token, so it never competes with the deployed worker for updates.

    uv run python -m telegram_stt.cli recording.m4a
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .archive import Archive
from .config import Config, ConfigError, load_dotenv
from .formatting import footer, progress_bar, render_transcript
from .transcribe import TranscriptionError, transcribe


def _progress(fraction: float) -> None:
    # Carriage return keeps the bar on one line; stderr leaves stdout clean
    # for the transcript itself.
    print(f"\r  {progress_bar(fraction, width=24)}", end="", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="telegram_stt.cli", description="Transcribe an audio file locally."
    )
    parser.add_argument("audio", type=Path, help="any file ffmpeg can decode")
    parser.add_argument("-o", "--output", type=Path, help="write transcript here")
    parser.add_argument(
        "--no-archive", action="store_true", help="skip writing to the archive"
    )
    parser.add_argument(
        "--no-timestamps", action="store_true", help="omit the [MM:SS] prefixes"
    )
    args = parser.parse_args(argv)

    app_dir = Path(__file__).resolve().parents[2]
    load_dotenv(app_dir / ".env")
    # The CLI never talks to Telegram, so a token is irrelevant — but Config
    # demands one. Satisfy it without requiring a real credential.
    import os

    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "cli:local")

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if not args.audio.is_file():
        print(f"no such file: {args.audio}", file=sys.stderr)
        return 1

    print(f"transcribing {args.audio.name}", file=sys.stderr)
    started = time.monotonic()
    try:
        result = transcribe(
            args.audio,
            model=config.whisper_model,
            language=config.whisper_language,
            initial_prompt=config.whisper_initial_prompt,
            max_seconds=config.max_audio_seconds,
            on_progress=_progress,
        )
    except TranscriptionError as exc:
        print(f"\nfailed: {exc}", file=sys.stderr)
        return 1
    print("", file=sys.stderr)

    body = render_transcript(
        result.segments,
        with_timestamps=config.show_timestamps and not args.no_timestamps,
    ) or result.text

    if not args.no_archive:
        archive = Archive(config.archive_dir, keep_audio=config.keep_audio)
        entry = archive.store(
            chat_id=0,
            message_id=int(started),
            source_audio=args.audio,
            transcript_text=body,
            media_kind="cli",
            original_name=args.audio.name,
            file_unique_id=None,
            language=result.language,
            model=config.whisper_model,
            audio_seconds=result.audio_seconds,
            elapsed_seconds=result.elapsed_seconds,
            segments=result.segments,
        )
        print(f"archived -> {entry.text_path}", file=sys.stderr)

    if args.output:
        args.output.write_text(body, encoding="utf-8")
        print(f"wrote    -> {args.output}", file=sys.stderr)
    else:
        print(body)

    print(
        footer(
            config.whisper_model,
            result.language,
            result.audio_seconds,
            result.elapsed_seconds,
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
