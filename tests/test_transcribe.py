"""Decoding and guardrails. Only the marked test loads Whisper."""

import pytest

from telegram_stt.transcribe import (
    AudioTooLong, SAMPLE_RATE, TranscriptionError, decode_to_array, transcribe,
)


def test_decode_returns_a_mono_float32_waveform(tone_wav):
    waveform, duration = decode_to_array(tone_wav)
    assert duration == pytest.approx(2.0, abs=0.05)
    assert len(waveform) == pytest.approx(SAMPLE_RATE * 2, rel=0.01)
    assert waveform.dtype.name == "float32"
    assert -1.0 <= float(waveform.min()) and float(waveform.max()) <= 1.0


def test_decode_gives_a_useful_error_for_a_non_audio_file(tmp_path):
    junk = tmp_path / "notes.bin"
    junk.write_bytes(b"definitely not audio" * 50)
    with pytest.raises(TranscriptionError, match="could not decode"):
        decode_to_array(junk)


def test_decode_rejects_an_empty_file(tmp_path):
    empty = tmp_path / "empty.ogg"
    empty.write_bytes(b"")
    with pytest.raises(TranscriptionError):
        decode_to_array(empty)


def test_length_limit_is_enforced_on_the_decoded_audio(tone_wav):
    """Telegram omits `duration` on documents, so the caller's up-front check
    can be bypassed. This is the gate that actually holds."""
    with pytest.raises(AudioTooLong) as exc:
        transcribe(tone_wav, model="unused", max_seconds=1)
    assert "2s" in str(exc.value) and "1s" in str(exc.value)
    assert isinstance(exc.value, TranscriptionError)


def test_limit_of_zero_disables_the_check(tone_wav, monkeypatch):
    """max_seconds=0 must not reject everything."""
    calls = {}

    def fake(audio, **kwargs):
        calls["ran"] = True
        return {"text": "ok", "language": "en", "segments": []}

    import mlx_whisper
    monkeypatch.setattr(mlx_whisper, "transcribe", fake)
    transcribe(tone_wav, model="unused", max_seconds=0)
    assert calls.get("ran")


def test_silence_raises_rather_than_returning_an_empty_transcript(tone_wav, monkeypatch):
    import mlx_whisper
    monkeypatch.setattr(mlx_whisper, "transcribe",
                        lambda audio, **k: {"text": "   ", "language": "en", "segments": []})
    with pytest.raises(TranscriptionError, match="no speech"):
        transcribe(tone_wav, model="unused")


class TestProgressShim:
    """mlx-whisper drives an internal tqdm and exposes no callback, so we swap
    the module's tqdm reference. These tests are the canary for that trick."""

    def test_bar_reports_monotonic_fractions_reaching_one(self):
        from telegram_stt.transcribe import _ProgressTqdm

        seen = []
        bar = _ProgressTqdm(seen.append).tqdm(total=100, unit="frames")
        with bar:
            for _ in range(4):
                bar.update(25)
        assert seen == [0.25, 0.5, 0.75, 1.0]

    def test_bar_never_exceeds_one(self):
        from telegram_stt.transcribe import _ProgressTqdm

        seen = []
        bar = _ProgressTqdm(seen.append).tqdm(total=10)
        bar.update(50)
        assert seen == [1.0]

    def test_unknown_total_reports_nothing_rather_than_dividing_by_zero(self):
        from telegram_stt.transcribe import _ProgressTqdm

        seen = []
        bar = _ProgressTqdm(seen.append).tqdm(total=0)
        bar.update(5)
        assert seen == []

    def test_the_module_tqdm_is_swapped_in_and_always_restored(self, tone_wav, monkeypatch):
        import sys

        import mlx_whisper
        import mlx_whisper.transcribe  # noqa: F401

        module = sys.modules["mlx_whisper.transcribe"]
        original = module.tqdm
        observed = {}

        def fake(audio, **kwargs):
            # what mlx-whisper's own loop does, in miniature
            observed["tqdm"] = module.tqdm
            with module.tqdm.tqdm(total=4, unit="frames") as bar:
                bar.update(2)
                bar.update(2)
            return {"text": "done", "language": "en", "segments": []}

        monkeypatch.setattr(mlx_whisper, "transcribe", fake)
        seen = []
        transcribe(tone_wav, model="unused", on_progress=seen.append, max_seconds=0)

        assert observed["tqdm"] is not original, "our shim was never installed"
        assert seen == [0.5, 1.0]
        assert module.tqdm is original, "the real tqdm was not restored"

    def test_tqdm_is_restored_even_when_transcription_raises(self, tone_wav, monkeypatch):
        import sys

        import mlx_whisper
        import mlx_whisper.transcribe  # noqa: F401

        module = sys.modules["mlx_whisper.transcribe"]
        original = module.tqdm

        def boom(audio, **kwargs):
            raise RuntimeError("model exploded")

        monkeypatch.setattr(mlx_whisper, "transcribe", boom)
        with pytest.raises(RuntimeError):
            transcribe(tone_wav, model="unused", on_progress=lambda f: None, max_seconds=0)
        assert module.tqdm is original


@pytest.mark.slow
def test_real_speech_round_trip(tmp_path):
    """End-to-end against the actual model, using synthesised speech.

    Deselect with -m 'not slow'.
    """
    import shutil
    import subprocess

    if not shutil.which("say"):
        pytest.skip("needs macOS `say` to synthesise speech")
    aiff = tmp_path / "speech.aiff"
    subprocess.run(["say", "-o", str(aiff), "The quick brown fox jumps over the lazy dog."],
                   check=True)
    result = transcribe(aiff, model="mlx-community/whisper-tiny", max_seconds=0)
    assert "fox" in result.text.lower()
    assert result.segments and result.speed_factor > 0
