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
    # Whisper's own segmentation, each with "start", "end" and "text".
    segments: tuple[dict, ...] = ()

    @property
    def speed_factor(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.audio_seconds / self.elapsed_seconds


class _ProgressTqdm:
    """Stands in for tqdm inside mlx_whisper so we can watch its position.

    mlx_whisper drives a tqdm bar over audio frames in its decode loop and
    exposes no callback. Swapping the module reference for this shim turns
    those updates into a real progress fraction — no timer-based guessing.
    """

    def __init__(self, on_progress):
        self._on_progress = on_progress

    def tqdm(self, *args, total=None, **kwargs):
        return _ProgressBar(total, self._on_progress)


class _ProgressBar:
    def __init__(self, total, on_progress):
        self.total = total or 0
        self.n = 0
        self._on_progress = on_progress

    def update(self, amount=1):
        self.n += amount
        if self.total > 0:
            self._on_progress(min(1.0, self.n / self.total))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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

    Returns (waveform, duration_seconds) with the waveform as a numpy float32
    array — the one format both mlx-whisper and sherpa-onnx accept, so a job
    decodes its audio exactly once no matter how many engines consume it.

    Handing mlx_whisper a *path* would make it spawn its own ffmpeg to do this
    exact conversion again (see log_mel_spectrogram -> load_audio), so we do it
    once here and pass the array. Same peak memory as mlx's own loader, minus a
    subprocess and a temp file.
    """
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
    return (pcm.astype(np.float32) / 32768.0), duration


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
    on_progress=None,
    waveform=None,
) -> Transcript:
    import sys

    import mlx_whisper  # imported lazily — pulling in MLX takes a few seconds
    import mlx_whisper.transcribe  # noqa: F401  (ensure the submodule is loaded)

    # mlx_whisper.transcribe is the re-exported *function*, not the module that
    # owns the tqdm reference we need to swap; go through sys.modules for it.
    mlx_transcribe = sys.modules["mlx_whisper.transcribe"]

    started = time.monotonic()
    if waveform is None:
        waveform, audio_seconds = decode_to_array(src)
    else:
        audio_seconds = len(waveform) / float(SAMPLE_RATE)
    # Telegram omits `duration` on documents, so the caller's up-front check can
    # be bypassed by attaching a long file. This is the real gate: we now know
    # the true length and have not yet touched the GPU.
    if max_seconds and audio_seconds > max_seconds:
        raise AudioTooLong(audio_seconds, max_seconds)

    log.info("transcribing %.1fs of audio with %s", audio_seconds, model)
    original_tqdm = mlx_transcribe.tqdm
    if on_progress is not None:
        mlx_transcribe.tqdm = _ProgressTqdm(on_progress)
    try:
        result = mlx_whisper.transcribe(
            waveform,
            path_or_hf_repo=model,
            language=language,
            initial_prompt=initial_prompt,
            # Whisper loops on itself when it loses the thread; not carrying
            # context between windows keeps long recordings from degenerating.
            # Measured as both faster and more stable than the default.
            condition_on_previous_text=False,
            word_timestamps=False,
            # False (not None) is what un-disables mlx_whisper's internal bar.
            verbose=False if on_progress is not None else None,
        )
    finally:
        mlx_transcribe.tqdm = original_tqdm

    text = (result.get("text") or "").strip()
    if not text:
        raise TranscriptionError("no speech detected in the audio")

    return Transcript(
        text=text,
        language=result.get("language"),
        audio_seconds=audio_seconds,
        elapsed_seconds=time.monotonic() - started,
        segments=tuple(result.get("segments") or ()),
    )


def warmup(model: str) -> None:
    """Load and compile the model against a second of silence so the first real
    request does not eat the model download plus Metal warmup."""
    import mlx_whisper
    import numpy as np

    mlx_whisper.transcribe(
        np.zeros(SAMPLE_RATE, dtype=np.float32), path_or_hf_repo=model, verbose=None
    )
