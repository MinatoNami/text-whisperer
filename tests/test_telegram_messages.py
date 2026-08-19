"""What the bot actually says, and how summaries reach the chat."""

import json
import time

import pytest

from telegram_stt.bot import Bot
from telegram_stt.config import Config


class StubLLM:
    def __init__(self, summary="## Summary\nWe agreed to ship.\n\n## Key points\n- One\n- Two"):
        self.summary = summary
        self.calls = 0

    def summarise(self, text, on_progress=None):
        self.calls += 1
        if on_progress:
            on_progress(0.5, "part 1 of 2")
        return self.summary


@pytest.fixture
def bot(app_dir, telegram, fake_transcribe, monkeypatch):
    monkeypatch.setenv("BOT_API_BASE_URL", telegram.base_url)
    monkeypatch.setenv("AUTO_SUMMARIZE_OVER_SECONDS", "0")   # off unless a test opts in
    b = Bot(Config.from_env())
    b.llm = StubLLM()
    return b


def texts(items):
    return [i.get("text", "") for i in items]


class TestCopyIsHuman:
    def test_receipt_speaks_like_a_person(self, bot, telegram, run_bot_until_done):
        telegram.queue_audio(duration=3077)
        run_bot_until_done(bot, telegram)
        first = texts(telegram.sent)[0]
        assert "Got your 51 min recording" in first
        for jargon in ("Received voice", "KB", "decoding", "placeholder"):
            assert jargon not in first, f"{jargon!r} leaked into the greeting"

    def test_progress_has_no_block_art(self, bot, telegram, run_bot_until_done):
        telegram.queue_audio()
        run_bot_until_done(bot, telegram)
        joined = " ".join(texts(telegram.edits))
        assert "█" not in joined and "░" not in joined, "block bar still being sent"
        assert "Transcribing" in joined

    def test_progress_reports_a_percentage(self, bot, telegram, run_bot_until_done):
        telegram.queue_audio()
        run_bot_until_done(bot, telegram)
        assert any("%" in t for t in texts(telegram.edits))

    def test_caption_carries_no_telemetry(self, bot, telegram, run_bot_until_done):
        telegram.queue_audio()
        run_bot_until_done(bot, telegram)
        body = telegram.documents[0].decode("utf-8", "replace")
        for leak in ("whisper-large-v3-turbo", "mlx-community", "realtime", "×"):
            assert leak not in body, f"{leak!r} is telemetry, not a caption"

    def test_caption_says_length_and_language(self, bot, telegram, run_bot_until_done):
        telegram.queue_audio()
        run_bot_until_done(bot, telegram)
        body = telegram.documents[0].decode("utf-8", "replace")
        assert "recording" in body and "English" in body

    def test_failure_message_is_kind(self, app_dir, telegram, fake_transcribe,
                                     monkeypatch, run_bot_until_done):
        from telegram_stt import bot as bot_module
        from telegram_stt.transcribe import TranscriptionError

        monkeypatch.setenv("BOT_API_BASE_URL", telegram.base_url)
        monkeypatch.setattr(bot_module, "transcribe",
                            lambda *a, **k: (_ for _ in ()).throw(TranscriptionError("no audio")))
        b = Bot(Config.from_env())
        telegram.queue_audio()
        import threading
        threading.Thread(target=lambda: (time.sleep(3), b.stopping.set()), daemon=True).start()
        run_bot_until_done(b, telegram, timeout=3)
        assert any("couldn't transcribe" in t for t in texts(telegram.edits))
        assert not any("⚠️" in t for t in texts(telegram.edits))


class TestSummaryButton:
    def test_transcript_is_sent_with_a_summarise_button(self, bot, telegram, run_bot_until_done):
        telegram.queue_audio()
        run_bot_until_done(bot, telegram)
        body = telegram.documents[0].decode("utf-8", "replace")
        assert "inline_keyboard" in body and "Summarise" in body

    def test_tapping_it_posts_a_summary(self, bot, telegram, run_bot_until_done):
        telegram.queue_audio()
        run_bot_until_done(bot, telegram)
        stem = json.loads(
            (bot.config.archive_dir / "history.jsonl").read_text().strip().splitlines()[-1]
        )["text_file"].split("/")[-1][:-4]

        bot._deliver_summary(-100999, stem, reply_to=None)
        posted = " ".join(texts(telegram.sent) + texts(telegram.edits))
        assert "Summary" in posted and "We agreed to ship" in posted
        assert "<b>" in posted, "summary should use Telegram HTML, not raw markdown"
        assert "##" not in posted, "raw markdown headings leaked through"

    def test_a_cached_summary_does_not_call_the_model_again(self, bot, telegram,
                                                            run_bot_until_done):
        telegram.queue_audio()
        run_bot_until_done(bot, telegram)
        stem = json.loads(
            (bot.config.archive_dir / "history.jsonl").read_text().strip().splitlines()[-1]
        )["text_file"].split("/")[-1][:-4]
        bot._deliver_summary(-100999, stem, None)
        bot._deliver_summary(-100999, stem, None)
        assert bot.llm.calls == 1

    def test_callback_is_acknowledged_and_the_button_removed(self, bot, telegram):
        bot._handle_callback({"id": "cb1", "data": "sum:nope",
                              "message": {"message_id": 5, "chat": {"id": -100999}}})
        time.sleep(0.4)
        assert telegram.answered, "Telegram spins forever without answerCallbackQuery"
        assert telegram.markup_edits, "the button should be removed after tapping"

    def test_callback_from_a_disallowed_chat_is_refused(self, app_dir, telegram,
                                                        fake_transcribe, monkeypatch):
        monkeypatch.setenv("BOT_API_BASE_URL", telegram.base_url)
        monkeypatch.setenv("ALLOWED_CHAT_IDS", "-100999")
        b = Bot(Config.from_env())
        b._handle_callback({"id": "cb9", "data": "sum:x",
                            "message": {"message_id": 5, "chat": {"id": -777}}})
        assert telegram.answered[-1].get("text") == "Not allowed here."
        assert not telegram.markup_edits


class TestAutoSummary:
    def test_long_recordings_summarise_without_asking(self, app_dir, telegram,
                                                      fake_transcribe, monkeypatch,
                                                      run_bot_until_done):
        monkeypatch.setenv("BOT_API_BASE_URL", telegram.base_url)
        monkeypatch.setenv("AUTO_SUMMARIZE_OVER_SECONDS", "5")
        b = Bot(Config.from_env())
        b.llm = StubLLM()
        telegram.queue_audio(duration=3077)
        run_bot_until_done(b, telegram)
        assert b.llm.calls == 1
        assert any("We agreed to ship" in t for t in texts(telegram.sent) + texts(telegram.edits))

    def test_short_ones_are_left_alone(self, bot, telegram, run_bot_until_done, monkeypatch):
        monkeypatch.setenv("AUTO_SUMMARIZE_OVER_SECONDS", "600")
        b = Bot(Config.from_env())
        b.llm = StubLLM()
        telegram.queue_audio(duration=8)
        run_bot_until_done(b, telegram)
        assert b.llm.calls == 0, "a short voice note is its own summary"

    def test_a_very_long_summary_is_sent_as_a_file(self, app_dir, telegram, fake_transcribe,
                                                   monkeypatch, run_bot_until_done):
        monkeypatch.setenv("BOT_API_BASE_URL", telegram.base_url)
        monkeypatch.setenv("AUTO_SUMMARIZE_OVER_SECONDS", "5")
        b = Bot(Config.from_env())
        b.llm = StubLLM("## Summary\n" + ("a very wordy sentence. " * 400))
        telegram.queue_audio(duration=3077)
        run_bot_until_done(b, telegram)
        assert len(telegram.documents) == 2, "transcript plus a summary file"
        assert b"-summary.docx" in telegram.documents[1], "long summaries go as Word"
        # the multipart body should carry a real zip container
        assert b"PK\x03\x04" in telegram.documents[1]
