import pytest

from telegram_stt.formatting import (
    footer, human_duration, human_size, precise_duration, progress_bar,
    render_transcript, timestamp,
)


@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"), (9, "9s"), (59, "59s"), (60, "1m00s"), (192, "3m12s"),
    (3600, "1h00m00s"), (3730, "1h02m10s"),
])
def test_human_duration(seconds, expected):
    assert human_duration(seconds) == expected


def test_human_duration_never_negative():
    assert human_duration(-5) == "0s"


@pytest.mark.parametrize("size,expected", [
    (None, "unknown size"), (0, "unknown size"), (512, "512 B"),
    (27844, "27.2 KB"), (5 * 1024**2, "5.0 MB"), (3 * 1024**3, "3.0 GB"),
])
def test_human_size(size, expected):
    assert human_size(size) == expected


@pytest.mark.parametrize("seconds,expected", [
    (9, "00:09"), (65, "01:05"), (600, "10:00"), (3725, "1:02:05"),
])
def test_timestamp(seconds, expected):
    assert timestamp(seconds) == expected


def test_precise_duration_keeps_a_decimal_under_ten_seconds():
    # A sub-second job used to report as "0s", which read as broken.
    assert precise_duration(0.24) == "0.2s"
    assert precise_duration(120) == "2m00s"


def test_progress_bar_only_fills_at_a_true_hundred_percent():
    assert progress_bar(0.0).startswith("░")
    assert "█" * 12 not in progress_bar(0.99)
    assert progress_bar(1.0).startswith("█" * 12)


def test_progress_bar_clamps_out_of_range_input():
    assert "0%" in progress_bar(-3)
    assert "100%" in progress_bar(7)


class TestRenderTranscript:
    segments = [
        {"start": 0.0, "end": 4.0, "text": " First line."},
        {"start": 4.5, "end": 9.0, "text": " Second line."},
        {"start": 61.0, "end": 65.0, "text": " Third line."},
    ]

    def test_one_line_per_segment_with_timestamps(self):
        # Regression: an earlier version grouped segments and collapsed the
        # whole transcript onto a single line under one timestamp.
        out = render_transcript(self.segments).splitlines()
        assert out == ["[00:00] First line.", "[00:04] Second line.", "[01:01] Third line."]

    def test_timestamps_can_be_switched_off(self):
        out = render_transcript(self.segments, with_timestamps=False).splitlines()
        assert out == ["First line.", "Second line.", "Third line."]

    def test_blank_segments_are_dropped(self):
        out = render_transcript([{"start": 0, "end": 1, "text": "   "},
                                 {"start": 1, "end": 2, "text": "kept"}])
        assert out == "[00:01] kept"

    def test_no_segments_returns_empty_so_caller_can_fall_back(self):
        assert render_transcript([]) == ""


def test_footer_reports_speed_and_language():
    out = footer("mlx-community/whisper-large-v3-turbo", "en", 192, 18)
    assert "whisper-large-v3-turbo" in out and "mlx-community" not in out
    assert "en" in out and "3m12s audio" in out and "10.7×" in out


def test_footer_without_a_detected_language():
    assert "·" in footer("a/b", None, 10, 1)
