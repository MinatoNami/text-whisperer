"""Audio decoding (ffmpeg) and speech-to-text (MLX Whisper on the Apple GPU)."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    audio_seconds: float
    elapsed_seconds: float

    @property
    def speed_factor(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.audio_seconds / self.elapsed_seconds


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise TranscriptionError(
            f"{binary} not found on PATH. Install it with: brew install ffmpeg"
        )
    return path


def decode_to_array(src: Path):
    """Decode whatever container Telegram sends (ogg/opus, m4a, mp4, ...) into
    the 16 kHz mono float32 waveform Whisper wants, in memory.

    Returns (waveform, duration_seconds).

    Handing mlx_whisper a *path* would make it spawn its own ffmpeg to do this
    exact conversion again (see log_mel_spectrogram -> load_audio), so we do it
    once here and pass the array. Same peak memory as mlx's own loader, minus a
    subprocess and a temp file.
    """
    import mlx.core as mx
    import numpy as np

    ffmpeg = _require("ffmpeg")
    result = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(src),
            "-vn",
            "-threads", "0",
            "-f", "s16le",
            "-ac", "1",
            "-acodec", "pcm_s16le",
            "-ar", str(SAMPLE_RATE),
            "-",
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[-500:] or "no stderr"
        raise TranscriptionError(f"ffmpeg could not decode {src.name}: {detail}")

    pcm = np.frombuffer(result.stdout, np.int16)
    duration = len(pcm) / float(SAMPLE_RATE)
    if duration <= 0:
        raise TranscriptionError(f"{src.name} contains no audio")
    return mx.array(pcm).astype(mx.float32) / 32768.0, duration


class AudioTooLong(TranscriptionError):
    def __init__(self, seconds: float, limit: int):
        from .formatting import human_duration

        super().__init__(
            f"audio is {human_duration(seconds)} long; the limit is {human_duration(limit)}"
        )
        self.seconds = seconds
        self.limit = limit


def transcribe(
    src: Path,
    *,
    model: str,
    language: str | None = None,
    initial_prompt: str | None = None,
    max_seconds: int = 0,
) -> Transcript:
    import mlx_whisper  # imported lazily — pulling in MLX takes a few seconds

    started = time.monotonic()
    waveform, audio_seconds = decode_to_array(src)
    # Telegram omits `duration` on documents, so the caller's up-front check can
    # be bypassed by attaching a long file. This is the real gate: we now know
    # the true length and have not yet touched the GPU.
    if max_seconds and audio_seconds > max_seconds:
        raise AudioTooLong(audio_seconds, max_seconds)

    log.info("transcribing %.1fs of audio with %s", audio_seconds, model)
    result = mlx_whisper.transcribe(
        waveform,
        path_or_hf_repo=model,
        language=language,
        initial_prompt=initial_prompt,
        # Whisper loops on itself when it loses the thread; not carrying context
        # between windows keeps long recordings from degenerating. Measured as
        # both faster and more stable than the default on real speech.
        condition_on_previous_text=False,
        word_timestamps=False,
        verbose=None,
    )

    text = (result.get("text") or "").strip()
    if not text:
        raise TranscriptionError("no speech detected in the audio")

    return Transcript(
        text=text,
        language=result.get("language"),
        audio_seconds=audio_seconds,
        elapsed_seconds=time.monotonic() - started,
    )


def warmup(model: str) -> None:
    """Load and compile the model against a second of silence so the first real
    request does not eat the model download plus Metal warmup."""
    import mlx.core as mx
    import mlx_whisper

    mlx_whisper.transcribe(
        mx.zeros(SAMPLE_RATE, dtype=mx.float32), path_or_hf_repo=model, verbose=None
    )
