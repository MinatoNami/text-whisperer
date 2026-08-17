"""Speaker diarization — who spoke when — via sherpa-onnx.

Whisper has no notion of speakers, so this runs a separate pyannote
segmentation network plus a speaker-embedding model and clusters the result.
Both models are ungated and run on CPU, so nothing here needs credentials.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger(__name__)

SEGMENTATION_DIR = "sherpa-onnx-pyannote-segmentation-3-0"
SEGMENTATION_MODEL = f"{SEGMENTATION_DIR}/model.onnx"
EMBEDDING_MODEL = "wespeaker_en_voxceleb_CAM++.onnx"


class DiarizationUnavailable(RuntimeError):
    """Raised when diarization is requested but cannot run."""


@dataclass(frozen=True)
class SpeakerSegment:
    start: float
    end: float
    speaker: int

    def overlap(self, start: float, end: float) -> float:
        return max(0.0, min(self.end, end) - max(self.start, start))


def _link_onnxruntime() -> None:
    """Repair the sherpa-onnx wheel's missing runtime library.

    The macOS wheel links against `@rpath/libonnxruntime.dylib` but ships no
    such file, so importing it fails outright. The onnxruntime package we
    depend on does provide the dylib, just under a versioned name. Symlinking
    it into place is what makes the import work — and doing it here rather
    than in a deploy script means it self-heals after any `uv sync`.
    """
    try:
        import sherpa_onnx  # noqa: F401
        return  # already importable, nothing to repair
    except ImportError:
        pass

    try:
        import onnxruntime
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise DiarizationUnavailable(f"onnxruntime is not installed: {exc}") from exc

    capi = Path(onnxruntime.__file__).parent / "capi"
    candidates = sorted(capi.glob("libonnxruntime.*.dylib"))
    if not candidates:
        raise DiarizationUnavailable(f"no libonnxruntime dylib under {capi}")

    site_packages = Path(onnxruntime.__file__).parent.parent
    target_dir = site_packages / "sherpa_onnx" / "lib"
    if not target_dir.is_dir():
        raise DiarizationUnavailable(f"sherpa_onnx is not installed at {target_dir}")

    link = target_dir / "libonnxruntime.dylib"
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(candidates[-1])
        log.info("linked %s -> %s", link, candidates[-1].name)
    except OSError as exc:
        raise DiarizationUnavailable(f"could not link libonnxruntime: {exc}") from exc


class Diarizer:
    """Holds the loaded ONNX models. Building this is expensive; keep one."""

    def __init__(
        self,
        model_dir: Path,
        *,
        threshold: float = 0.7,
        num_speakers: int = 0,
        num_threads: int = 4,
    ):
        self.model_dir = model_dir
        self.threshold = threshold
        self.num_speakers = num_speakers
        self._impl = None
        self._num_threads = num_threads

    def _paths(self) -> tuple[Path, Path]:
        segmentation = self.model_dir / SEGMENTATION_MODEL
        embedding = self.model_dir / EMBEDDING_MODEL
        missing = [p for p in (segmentation, embedding) if not p.is_file()]
        if missing:
            raise DiarizationUnavailable(
                "diarization models are missing: "
                + ", ".join(str(p) for p in missing)
                + " — run scripts/fetch-models.sh"
            )
        return segmentation, embedding

    def _build(self):
        if self._impl is not None:
            return self._impl

        if sys.platform == "darwin":
            _link_onnxruntime()
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise DiarizationUnavailable(f"sherpa-onnx is not usable: {exc}") from exc

        segmentation, embedding = self._paths()
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(segmentation)
                ),
                num_threads=self._num_threads,
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(embedding), num_threads=self._num_threads
            ),
            # num_clusters=-1 lets the threshold decide how many speakers there
            # are; a positive NUM_SPEAKERS pins it when the count is known.
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=self.num_speakers if self.num_speakers > 0 else -1,
                threshold=self.threshold,
            ),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not config.validate():
            raise DiarizationUnavailable("sherpa-onnx rejected the diarization config")

        self._impl = sherpa_onnx.OfflineSpeakerDiarization(config)
        return self._impl

    @property
    def sample_rate(self) -> int:
        return self._build().sample_rate

    def run(
        self, waveform, on_progress: Callable[[float], None] | None = None
    ) -> list[SpeakerSegment]:
        """Diarize a 16 kHz mono float32 waveform (numpy array)."""
        impl = self._build()

        def callback(processed: int, total: int, *_) -> int:
            if on_progress and total:
                on_progress(processed / total)
            return 0

        result = impl.process(waveform, callback=callback if on_progress else None)
        return [
            SpeakerSegment(start=s.start, end=s.end, speaker=s.speaker)
            for s in result.sort_by_start_time()
        ]

    def warmup(self) -> None:
        self._build()


def assign_speakers(
    transcript_segments: Sequence[dict], speakers: Sequence[SpeakerSegment]
) -> list[int | None]:
    """Label each transcript segment with the speaker who overlaps it most.

    Whisper and the diarizer segment audio independently, so boundaries never
    line up exactly. Maximum temporal overlap is the standard reconciliation.
    """
    if not speakers:
        return [None] * len(transcript_segments)

    labels: list[int | None] = []
    for segment in transcript_segments:
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        best: int | None = None
        best_overlap = 0.0
        for candidate in speakers:
            overlap = candidate.overlap(start, end)
            if overlap > best_overlap:
                best_overlap, best = overlap, candidate.speaker
        if best is None:
            # No overlap at all (Whisper heard speech the diarizer called
            # silence) — fall back to the nearest speaker turn.
            best = min(
                speakers,
                key=lambda c: min(abs(c.start - start), abs(c.end - end)),
            ).speaker
        labels.append(best)
    return labels
