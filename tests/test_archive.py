import json

import pytest

from telegram_stt.archive import Archive

SEGMENTS = [{"start": 0.0, "end": 4.0, "text": " hello"},
            {"start": 4.0, "end": 8.0, "text": " world"}]


def store_one(archive, **overrides):
    kwargs = dict(chat_id=-100999, message_id=42, transcript_text="[00:00] hello\n[00:04] world",
                  media_kind="voice", original_name="meeting.m4a", language="en",
                  model="mlx-community/whisper-large-v3-turbo", audio_seconds=8.0,
                  elapsed_seconds=0.5, segments=SEGMENTS)
    kwargs.update(overrides)
    return archive.store(**kwargs)


def test_store_writes_audio_transcript_and_metadata(tmp_path, tone_wav):
    a = Archive(tmp_path / "archive")
    entry = store_one(a, source_audio=tone_wav)
    assert entry.audio_path.is_file() and entry.text_path.is_file() and entry.meta_path.is_file()
    assert entry.text_path.read_text() == "[00:00] hello\n[00:04] world"
    assert json.loads(entry.meta_path.read_text())["segments"][1]["text"] == "world"


def test_files_are_grouped_by_day_and_share_a_stem(tmp_path, tone_wav):
    a = Archive(tmp_path / "archive")
    entry = store_one(a, source_audio=tone_wav)
    assert entry.audio_path.parent.name == entry.day
    stems = {p.stem for p in (entry.audio_path, entry.text_path, entry.meta_path)}
    assert len(stems) == 1


def test_keep_audio_false_skips_the_recording(tmp_path, tone_wav):
    a = Archive(tmp_path / "archive", keep_audio=False)
    entry = store_one(a, source_audio=tone_wav)
    assert entry.audio_path is None
    assert entry.record["audio_file"] is None
    assert entry.text_path.is_file()


def test_index_is_append_only(tmp_path, tone_wav):
    a = Archive(tmp_path / "archive")
    store_one(a, source_audio=tone_wav, message_id=1)
    store_one(a, source_audio=tone_wav, message_id=2)
    lines = a.index_path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert [json.loads(x)["message_id"] for x in lines] == [1, 2]


def test_a_torn_final_line_does_not_break_reading(tmp_path, tone_wav):
    a = Archive(tmp_path / "archive")
    store_one(a, source_audio=tone_wav)
    with a.index_path.open("a") as fh:
        fh.write('{"partial": tru')       # simulate a crash mid-append
    assert a.stats()["count"] == 1
    assert len(a.records()) == 1


def test_stats_totals(tmp_path, tone_wav):
    a = Archive(tmp_path / "archive")
    store_one(a, source_audio=tone_wav, message_id=1, audio_seconds=10)
    store_one(a, source_audio=tone_wav, message_id=2, audio_seconds=20)
    s = a.stats()
    assert s["count"] == 2 and s["audio_seconds"] == 30


def test_recent_is_newest_first(tmp_path, tone_wav):
    a = Archive(tmp_path / "archive")
    for i in (1, 2, 3):
        store_one(a, source_audio=tone_wav, message_id=i)
    assert [r["message_id"] for r in a.recent(2)] == [3, 2]


def test_find_by_stem(tmp_path, tone_wav):
    a = Archive(tmp_path / "archive")
    entry = store_one(a, source_audio=tone_wav)
    assert a.find(entry.stem)["message_id"] == 42
    assert a.find("no-such-stem") is None


class TestResolveRefusesEscapes:
    """The download endpoint turns index paths into files; it must stay caged."""

    @pytest.fixture
    def archive(self, tmp_path, tone_wav):
        a = Archive(tmp_path / "archive")
        store_one(a, source_audio=tone_wav)
        (tmp_path / "outside.txt").write_text("should never be served")
        return a

    def test_a_legitimate_relative_path_resolves(self, archive):
        record = archive.records()[0]
        assert archive.resolve(record["text_file"]).is_file()

    @pytest.mark.parametrize("path", [
        "../outside.txt",
        "../../etc/passwd",
        "/etc/passwd",
        "2026-01-01/../../../outside.txt",
    ])
    def test_traversal_is_refused(self, archive, path):
        assert archive.resolve(path) is None

    def test_empty_and_missing_paths_are_refused(self, archive):
        assert archive.resolve("") is None
        assert archive.resolve(None) is None
        assert archive.resolve("2026-01-01/nope.txt") is None


def test_disk_usage_counts_stored_bytes(tmp_path, tone_wav):
    a = Archive(tmp_path / "archive")
    store_one(a, source_audio=tone_wav)
    assert a.disk_usage() > tone_wav.stat().st_size
