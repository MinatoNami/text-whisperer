"""Recognising a file that has already been transcribed."""

import json

import pytest

from telegram_stt.archive import Archive
from telegram_stt.bot import Bot
from telegram_stt.config import Config
from telegram_stt.media import Media, extract_media


class TestExtraction:
    def test_the_stable_id_is_captured(self):
        media = extract_media({"voice": {"file_id": "A", "file_unique_id": "U1", "duration": 5}})
        assert media.file_unique_id == "U1"

    def test_a_payload_without_one_is_still_accepted(self):
        """Older archives and odd clients may not supply it."""
        assert extract_media({"voice": {"file_id": "A"}}).file_unique_id is None

    def test_it_survives_the_disk_round_trip(self):
        original = Media("F", "audio", 900, "m.m4a", 1, file_unique_id="U9")
        assert Media.from_dict(original.to_dict()).file_unique_id == "U9"


class TestArchiveLookup:
    @pytest.fixture
    def archive(self, tmp_path, tone_wav):
        a = Archive(tmp_path / "archive")
        a.store(chat_id=-1, message_id=1, source_audio=tone_wav,
                transcript_text="[00:00] hello", media_kind="voice",
                original_name="meeting.m4a", file_unique_id="UNIQ", language="en",
                model="m", audio_seconds=8.0, elapsed_seconds=1.0,
                segments=[{"start": 0.0, "end": 8.0, "text": "hello"}])
        return a

    def test_finds_a_previous_run_of_the_same_file(self, archive):
        assert archive.find_by_unique_id("UNIQ")["message_id"] == 1

    def test_a_different_file_is_not_a_duplicate(self, archive):
        assert archive.find_by_unique_id("SOMETHING-ELSE") is None

    def test_no_id_is_never_a_duplicate(self, archive):
        assert archive.find_by_unique_id(None) is None
        assert archive.find_by_unique_id("") is None

    def test_a_deleted_transcript_is_not_treated_as_a_duplicate(self, archive):
        """Otherwise a pruned archive would refuse to redo the work."""
        record = archive.records()[0]
        archive.resolve(record["text_file"]).unlink()
        assert archive.find_by_unique_id("UNIQ") is None

    def test_the_most_recent_run_wins(self, archive, tone_wav):
        archive.store(chat_id=-1, message_id=2, source_audio=tone_wav,
                      transcript_text="[00:00] newer", media_kind="voice",
                      original_name="meeting.m4a", file_unique_id="UNIQ", language="en",
                      model="m", audio_seconds=8.0, elapsed_seconds=1.0,
                      segments=[{"start": 0.0, "end": 8.0, "text": "newer"}])
        assert archive.find_by_unique_id("UNIQ")["message_id"] == 2


class TestEndToEnd:
    def test_a_resend_is_answered_without_transcribing_again(
        self, app_dir, telegram, fake_transcribe, monkeypatch, run_bot_until_done
    ):
        monkeypatch.setenv("BOT_API_BASE_URL", telegram.base_url)
        bot = Bot(Config.from_env())

        telegram.queue_audio(message_id=55)
        run_bot_until_done(bot, telegram)
        assert len(fake_transcribe) == 1, "the first send should transcribe"
        first_docs = len(telegram.documents)

        # same file_unique_id, new message — exactly what a re-send looks like
        telegram.delivered.clear()
        telegram.queue_audio(message_id=56)
        run_bot_until_done(Bot(Config.from_env()), telegram)

        assert len(fake_transcribe) == 1, "the re-send was transcribed again"
        assert len(telegram.documents) == first_docs + 1, "no transcript came back"
        body = telegram.documents[-1].decode("utf-8", "replace")
        assert "Already transcribed" in body
        assert "Summarise this" in body, "the summary button should still be offered"

    def test_only_one_archive_entry_is_written(
        self, app_dir, telegram, fake_transcribe, monkeypatch, run_bot_until_done
    ):
        monkeypatch.setenv("BOT_API_BASE_URL", telegram.base_url)
        telegram.queue_audio(message_id=55)
        run_bot_until_done(Bot(Config.from_env()), telegram)
        telegram.delivered.clear()
        telegram.queue_audio(message_id=56)
        run_bot_until_done(Bot(Config.from_env()), telegram)

        index = app_dir / "data" / "archive" / "history.jsonl"
        rows = [json.loads(l) for l in index.read_text().splitlines() if l.strip()]
        assert len(rows) == 1, "a re-send should not add a second archive entry"

    def test_it_can_be_switched_off(
        self, app_dir, telegram, fake_transcribe, monkeypatch, run_bot_until_done
    ):
        monkeypatch.setenv("BOT_API_BASE_URL", telegram.base_url)
        monkeypatch.setenv("SKIP_DUPLICATES", "0")
        # The stand-in server points at one real file; without this the first
        # run deletes it and the second has nothing to read. A real Bot API
        # server re-fetches per file_id, so this is a fixture detail only.
        monkeypatch.setenv("DELETE_MEDIA_AFTER", "0")
        telegram.queue_audio(message_id=55)
        run_bot_until_done(Bot(Config.from_env()), telegram)
        telegram.delivered.clear()
        telegram.queue_audio(message_id=56)
        run_bot_until_done(Bot(Config.from_env()), telegram)
        assert len(fake_transcribe) == 2, "with the check off it should redo the work"
