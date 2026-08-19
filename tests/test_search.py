"""Full-text search across archived transcripts."""

import pytest

from telegram_stt.archive import Archive

SEGMENTS_A = [
    {"start": 0.0, "end": 4.0, "text": " We need a rollback plan for the migration."},
    {"start": 12.0, "end": 16.0, "text": " The caching layer looks solid."},
    {"start": 65.0, "end": 70.0, "text": " Rollback is the risky part."},
]
SEGMENTS_B = [
    {"start": 0.0, "end": 5.0, "text": " Quarterly revenue was up."},
    {"start": 30.0, "end": 34.0, "text": " Starcom will handle paid social."},
]


@pytest.fixture
def archive(tmp_path, tone_wav):
    a = Archive(tmp_path / "archive")
    for msg, segs in ((1, SEGMENTS_A), (2, SEGMENTS_B)):
        a.store(chat_id=-1, message_id=msg, source_audio=tone_wav,
                transcript_text="\n".join(s["text"].strip() for s in segs),
                media_kind="voice", original_name=f"meeting-{msg}.m4a", language="en",
                model="m", audio_seconds=70.0, elapsed_seconds=1.0, segments=segs)
    return a


class TestSearch:
    def test_finds_the_transcript_containing_a_term(self, archive):
        results = archive.search("rollback")
        assert len(results) == 1
        assert results[0]["original_name"] == "meeting-1.m4a"

    def test_is_case_insensitive(self, archive):
        assert archive.search("ROLLBACK")[0]["total_matches"] == 2

    def test_reports_every_matching_segment_with_its_timestamp(self, archive):
        matches = archive.search("rollback")[0]["matches"]
        assert [m["start"] for m in matches] == [0.0, 65.0]

    def test_multiple_terms_are_and_not_or(self, archive):
        assert archive.search("rollback caching")            # both in meeting-1
        assert archive.search("rollback revenue") == []      # split across two files

    def test_a_term_in_the_other_transcript(self, archive):
        results = archive.search("Starcom")
        assert len(results) == 1 and results[0]["original_name"] == "meeting-2.m4a"

    def test_no_match_returns_nothing(self, archive):
        assert archive.search("kubernetes") == []

    def test_an_empty_query_returns_nothing_rather_than_everything(self, archive):
        assert archive.search("") == []
        assert archive.search("   ") == []

    def test_results_are_newest_first(self, archive):
        results = archive.search("a")            # matches both
        stamps = [r["timestamp"] for r in results]
        assert stamps == sorted(stamps, reverse=True)

    def test_per_record_match_cap_is_reported_honestly(self, archive):
        result = archive.search("rollback", per_record=1)[0]
        assert len(result["matches"]) == 1
        assert result["total_matches"] == 2, "the true count must survive truncation"

    def test_search_falls_back_to_the_flat_transcript(self, archive):
        """An archive missing its per-segment metadata must stay searchable."""
        for record in archive.records():
            path = archive.resolve(record.get("meta_file"))
            if path:
                path.unlink()
        results = archive.search("rollback")
        assert len(results) == 1
        assert results[0]["matches"], "fell back but found no lines"


class TestSummaryStorage:
    def test_summary_round_trip(self, archive):
        record = archive.records()[0]
        assert archive.has_summary(record) is False
        assert archive.read_summary(record) is None
        path = archive.write_summary(record, "## Summary\nIt happened.")
        assert path.name.endswith(".summary.md")
        assert archive.has_summary(record) is True
        assert archive.read_summary(record) == "## Summary\nIt happened."

    def test_summary_sits_beside_its_transcript(self, archive):
        record = archive.records()[0]
        path = archive.write_summary(record, "x")
        assert path.parent == archive.resolve(record["text_file"]).parent

    def test_summary_path_refuses_to_escape_the_archive(self, archive):
        """A crafted text_file in the index must not place a summary outside
        the archive root. Caught by Copilot Autofix on PR #2 -- summary_path
        built its path without the check resolve() already had."""
        for bad in ("../../../../tmp/evil.txt", "/etc/passwd", "a/../../../out.txt"):
            assert archive.summary_path({"text_file": bad}) is None, bad

    def test_summary_path_accepts_a_legitimate_record(self, archive):
        record = archive.records()[0]
        path = archive.summary_path(record)
        assert path is not None
        assert path.is_relative_to(archive.root.resolve())
        assert path.name.endswith(".summary.md")

    def test_summary_path_without_a_text_file_is_none(self, archive):
        assert archive.summary_path({}) is None

    def test_rewriting_replaces_rather_than_appends(self, archive):
        record = archive.records()[0]
        archive.write_summary(record, "first")
        archive.write_summary(record, "second")
        assert archive.read_summary(record) == "second"
