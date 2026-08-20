"""The whole path, against a stand-in Bot API server.

Transcription is stubbed, so these run in milliseconds and assert on control
flow: acknowledgements, progress, the uploaded file, and the archive.
"""

import json

import pytest

from telegram_stt.bot import Bot
from telegram_stt.config import Config


@pytest.fixture
def bot(app_dir, telegram, fake_transcribe, monkeypatch):
    monkeypatch.setenv("BOT_API_BASE_URL", telegram.base_url)
    return Bot(Config.from_env())


def uploaded_text(telegram):
    """Pull the .txt out of the multipart sendDocument body."""
    body = telegram.documents[0].decode("utf-8", "replace")
    start = body.find("filename=")
    return body[body.find("\r\n\r\n", start) + 4:].split("\r\n--")[0]


class TestHappyPath:
    def test_three_stages_then_a_txt_upload(self, bot, telegram, run_bot_until_done):
        telegram.queue_audio()
        run_bot_until_done(bot, telegram)

        assert any("Got your" in m["text"] for m in telegram.sent), "no receipt ack"
        assert any("Transcribing" in e["text"] for e in telegram.edits), "no progress shown"
        assert telegram.documents, "no transcript was uploaded"

    def test_progress_is_reported_as_a_percentage(self, bot, telegram, run_bot_until_done):
        """Block-character bars read as a glitch in a chat client, so progress
        is plain language now."""
        telegram.queue_audio()
        run_bot_until_done(bot, telegram)
        progress = [e["text"] for e in telegram.edits if "%" in e["text"]]
        assert progress, "no progress was ever reported"
        assert not any("█" in e["text"] for e in telegram.edits)

    def test_uploaded_file_is_the_transcript_and_nothing_else(self, bot, telegram, run_bot_until_done):
        telegram.queue_audio()
        run_bot_until_done(bot, telegram)
        text = uploaded_text(telegram)
        assert "[00:00]" in text, "timestamps missing"
        for banned in ("Source:", "Model:", "Duration:", "Language:", "Speakers:"):
            assert banned not in text, f"{banned!r} leaked into the transcript file"

    def test_status_message_is_removed_once_the_file_lands(self, bot, telegram, run_bot_until_done):
        telegram.queue_audio()
        run_bot_until_done(bot, telegram)
        assert telegram.deleted, "the status message was left behind"

    def test_the_job_is_archived_with_an_index_entry(self, bot, telegram, app_dir, run_bot_until_done):
        telegram.queue_audio()
        run_bot_until_done(bot, telegram)
        index = app_dir / "data" / "archive" / "history.jsonl"
        assert index.is_file()
        record = json.loads(index.read_text().strip().splitlines()[-1])
        assert record["language"] == "en" and record["characters"] > 0
        assert (app_dir / "data" / "archive" / record["text_file"]).is_file()

    def test_pending_queue_is_empty_afterwards(self, bot, telegram, app_dir, run_bot_until_done):
        telegram.queue_audio()
        run_bot_until_done(bot, telegram)
        assert json.loads((app_dir / "data" / "pending.json").read_text()) == {}

    def test_update_offset_is_persisted(self, bot, telegram, app_dir, run_bot_until_done):
        telegram.queue_audio()
        run_bot_until_done(bot, telegram)
        assert json.loads((app_dir / "data" / "state.json").read_text())["offset"] == 7001


class TestAccessControl:
    def test_messages_from_other_chats_are_ignored(
        self, app_dir, telegram, fake_transcribe, monkeypatch, run_bot_until_done
    ):
        monkeypatch.setenv("BOT_API_BASE_URL", telegram.base_url)
        monkeypatch.setenv("ALLOWED_CHAT_IDS", "-100999")
        bot = Bot(Config.from_env())
        telegram.queue_audio(chat_id=-777, message_id=1)
        telegram.queue_audio(chat_id=-100999, message_id=2)
        run_bot_until_done(bot, telegram)
        assert len(telegram.documents) == 1
        assert all(m["chat_id"] == -100999 for m in telegram.sent)

    def test_an_empty_allowlist_permits_everyone(self, bot, telegram, run_bot_until_done):
        telegram.queue_audio(chat_id=-424242)
        run_bot_until_done(bot, telegram)
        assert telegram.documents


class TestNonAudio:
    def test_plain_text_produces_no_job(self, bot, telegram, run_bot_until_done):
        telegram.queue_text("just chatting")
        telegram.queue_audio(message_id=99)
        run_bot_until_done(bot, telegram)
        assert len(telegram.documents) == 1

    def test_help_command_is_answered(self, bot, telegram, run_bot_until_done):
        telegram.queue_text("/help")
        telegram.queue_audio(message_id=99)
        run_bot_until_done(bot, telegram)
        assert any("transcribe" in m["text"].lower() for m in telegram.sent)

    def test_status_command_reports_the_chat_id(self, bot, telegram, run_bot_until_done):
        telegram.queue_text("/status")
        telegram.queue_audio(message_id=99)
        run_bot_until_done(bot, telegram)
        assert any("Chat ID: -100999" in m["text"] for m in telegram.sent)


class TestFailures:
    def test_a_too_long_recording_is_rejected_before_any_work(
        self, app_dir, telegram, fake_transcribe, monkeypatch, run_bot_until_done
    ):
        monkeypatch.setenv("BOT_API_BASE_URL", telegram.base_url)
        monkeypatch.setenv("MAX_AUDIO_SECONDS", "10")
        bot = Bot(Config.from_env())
        telegram.queue_audio(duration=99999)
        telegram.queue_audio(message_id=56, duration=5)
        run_bot_until_done(bot, telegram)
        assert any("limit is" in m["text"] for m in telegram.sent)

    def test_a_transcription_error_is_reported_and_clears_the_queue(
        self, app_dir, telegram, fake_transcribe, monkeypatch, run_bot_until_done
    ):
        from telegram_stt import bot as bot_module
        from telegram_stt.transcribe import TranscriptionError

        monkeypatch.setenv("BOT_API_BASE_URL", telegram.base_url)

        def boom(*a, **k):
            raise TranscriptionError("ffmpeg could not decode it")

        monkeypatch.setattr(bot_module, "transcribe", boom)
        bot = Bot(Config.from_env())
        telegram.queue_audio()

        import threading, time
        threading.Thread(target=lambda: (time.sleep(3), bot.stopping.set()), daemon=True).start()
        run_bot_until_done(bot, telegram, timeout=3)

        assert any("couldn't transcribe" in e["text"] for e in telegram.edits)
        # a failed job must not be retried forever on every restart
        assert json.loads((app_dir / "data" / "pending.json").read_text()) == {}
