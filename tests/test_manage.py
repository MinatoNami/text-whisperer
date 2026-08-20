"""Managing recordings: titles, tags, deletion and retention."""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from telegram_stt.archive import Archive
from telegram_stt.llm import parse_description


def post(base, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{base}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def get(base, path):
    with urllib.request.urlopen(f"{base}{path}", timeout=15) as response:
        return json.loads(response.read())


@pytest.fixture
def archive(tmp_path, tone_wav):
    a = Archive(tmp_path / "archive")
    a.store(chat_id=-1, message_id=1, source_audio=tone_wav,
            transcript_text="[00:00] hello", media_kind="voice",
            original_name="71 Robinson Rd 21.m4a", language="en", model="m",
            audio_seconds=3077.0, elapsed_seconds=30.0,
            segments=[{"start": 0.0, "end": 1.0, "text": "hello"}])
    return a


class TestMetadata:
    def test_a_new_recording_has_no_title_or_tags(self, archive):
        record = archive.records()[0]
        assert record["title"] == "" and record["tags"] == []

    def test_setting_a_title_survives_a_reread(self, archive):
        record = archive.records()[0]
        archive.set_meta(record, title="Starcom kickoff")
        assert archive.records()[0]["title"] == "Starcom kickoff"

    def test_the_index_itself_is_never_rewritten(self, archive):
        """history.jsonl is append-only; edits live in a sidecar."""
        before = archive.index_path.read_bytes()
        archive.set_meta(archive.records()[0], title="Renamed", tags=["starcom"])
        assert archive.index_path.read_bytes() == before

    def test_unreadable_metadata_does_not_hide_a_recording(self, archive):
        from telegram_stt import meta as meta_store

        record = archive.records()[0]
        meta_store.path_for(archive.root, record["text_file"]).write_text("{ broken")
        assert len(archive.records()) == 1, "a bad sidecar swallowed the recording"
        assert archive.records()[0]["title"] == ""

    def test_metadata_cannot_escape_the_archive(self, archive):
        from telegram_stt import meta as meta_store

        assert meta_store.path_for(archive.root, "../../outside.txt") is None


class TestDeletion:
    def test_delete_hides_it_but_keeps_the_files(self, archive):
        record = archive.records()[0]
        files = archive.files_for(record)
        archive.set_meta(record, deleted=True)
        assert archive.records() == []
        assert archive.records(include_deleted=True)[0]["deleted"] is True
        assert all(f.is_file() for f in files), "soft delete removed files"

    def test_restore_brings_it_back(self, archive):
        record = archive.records()[0]
        archive.set_meta(record, deleted=True)
        archive.set_meta(archive.records(include_deleted=True)[0], deleted=False)
        assert len(archive.records()) == 1

    def test_purge_erases_the_files_and_keeps_it_hidden(self, archive):
        record = archive.records()[0]
        files = archive.files_for(record)
        removed = archive.purge(record)
        assert removed >= 3, f"only removed {removed}"
        assert not any(f.is_file() for f in files if f.name.endswith((".m4a", ".wav", ".txt", ".json"))
                       and not f.name.endswith(".meta.json"))
        # The append-only index still has the line; the tombstone keeps it out.
        assert archive.records() == []

    def test_a_deleted_recording_is_not_a_duplicate_match(self, archive):
        """Otherwise deleting something would make it un-redoable."""
        record = archive.records()[0]
        archive.purge(record)
        assert archive.find_by_unique_id(record.get("file_unique_id")) is None


class TestRetention:
    def _age(self, archive, days):
        """Rewrite the index with an older timestamp, to test pruning."""
        rows = [json.loads(l) for l in archive.index_path.read_text().splitlines() if l.strip()]
        when = datetime.now(timezone.utc).astimezone() - timedelta(days=days)
        for r in rows:
            r["timestamp"] = when.isoformat(timespec="seconds")
        archive.index_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def test_recent_audio_is_kept(self, archive):
        removed, _ = archive.prune_audio(30)
        assert removed == 0
        assert archive.resolve(archive.records()[0]["audio_file"])

    def test_old_audio_is_dropped_but_the_transcript_stays(self, archive):
        self._age(archive, 120)
        removed, freed = archive.prune_audio(90)
        assert removed == 1 and freed > 0
        record = archive.records()[0]
        assert archive.resolve(record["audio_file"]) is None, "audio should be gone"
        assert archive.resolve(record["text_file"]), "the transcript must survive"
        assert record["audio_pruned"] is True

    def test_zero_days_never_prunes(self, archive):
        self._age(archive, 3650)
        assert archive.prune_audio(0) == (0, 0)

    def test_pruning_twice_is_harmless(self, archive):
        self._age(archive, 120)
        archive.prune_audio(90)
        assert archive.prune_audio(90) == (0, 0)


class TestTitlesFromTheModel:
    def test_parses_a_well_formed_reply(self):
        out = parse_description('{"title": "Starcom kickoff", "tags": ["starcom", "ncss"]}')
        assert out["title"] == "Starcom kickoff" and out["tags"] == ["starcom", "ncss"]

    def test_tolerates_fences_and_prose(self):
        out = parse_description('Sure:\n```json\n{"title": "Batch plan", "tags": ["lg"]}\n```')
        assert out["title"] == "Batch plan"

    def test_rejects_a_sentence_pretending_to_be_a_title(self):
        out = parse_description(json.dumps({"title": "x" * 100, "tags": []}))
        assert out["title"] == ""

    def test_garbage_yields_nothing_rather_than_raising(self):
        assert parse_description("I could not do that") == {}

    def test_caps_the_number_of_tags(self):
        out = parse_description(json.dumps({"title": "T", "tags": list("abcdefgh")}))
        assert len(out["tags"]) == 4


class TestApi:
    def test_rename_and_tag_through_the_api(self, server):
        base, bot = server
        stem = get(base, "/api/history")["records"][0]["id"]
        post(base, f"/api/record/{stem}/update", {"title": "Board sync", "tags": ["NCSS", "ncss", " lg "]})
        record = get(base, "/api/history")["records"][0]
        assert record["title"] == "Board sync"
        assert record["tags"] == ["lg", "ncss"], "tags should be lowercased and de-duplicated"

    def test_delete_then_restore_through_the_api(self, server):
        base, _ = server
        stem = get(base, "/api/history")["records"][0]["id"]
        post(base, f"/api/record/{stem}/delete")
        assert get(base, "/api/history")["records"] == []
        assert get(base, "/api/history?deleted=1")["records"][0]["id"] == stem
        post(base, f"/api/record/{stem}/restore")
        assert len(get(base, "/api/history")["records"]) == 1

    def test_an_empty_update_is_rejected(self, server):
        base, _ = server
        stem = get(base, "/api/history")["records"][0]["id"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(base, f"/api/record/{stem}/update", {})
        assert exc.value.code == 400

    def test_an_unknown_action_404s(self, server):
        base, _ = server
        stem = get(base, "/api/history")["records"][0]["id"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(base, f"/api/record/{stem}/frobnicate")
        assert exc.value.code == 404

    def test_describing_something_unsummarised_is_a_conflict(self, server):
        base, _ = server
        stem = get(base, "/api/history")["records"][0]["id"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(base, f"/api/record/{stem}/describe")
        assert exc.value.code == 409

    def test_describe_titles_a_summarised_recording(self, server):
        base, bot = server

        class Stub:
            def describe(self, summary):
                return {"title": "Trailer batch plan", "tags": ["lg", "starcom"]}

        bot.llm = Stub()
        bot.archive.write_summary(bot.archive.records()[0], "## Summary\nWe agreed.")
        stem = get(base, "/api/history")["records"][0]["id"]
        out = post(base, f"/api/record/{stem}/describe")
        assert out["title"] == "Trailer batch plan"
        assert get(base, "/api/history")["records"][0]["tags"] == ["lg", "starcom"]
