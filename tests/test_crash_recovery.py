"""A job accepted but not finished must survive the process dying.

The poll loop advances its Telegram offset when a message is *queued*, so
Telegram will not resend it. If the process dies with that job still in the
queue, the on-disk pending record is the only way back to it.
"""

import json
import threading
import time

import pytest

from telegram_stt.bot import Bot, Job
from telegram_stt.config import Config
from telegram_stt.media import Media


@pytest.fixture
def bot_factory(app_dir, telegram, fake_transcribe, monkeypatch):
    monkeypatch.setenv("BOT_API_BASE_URL", telegram.base_url)
    return lambda: Bot(Config.from_env())


def test_job_is_recorded_before_it_is_queued(bot_factory, telegram, app_dir):
    """The window between accepting and finishing must be covered on disk."""
    bot = bot_factory()
    bot._connect()
    bot.client.delete_webhook()
    telegram.queue_audio()

    # poll once, but never start the worker -- the job sits in the queue
    threading.Thread(target=lambda: (time.sleep(1.5), bot.stopping.set()), daemon=True).start()
    bot._poll_loop()

    pending = json.loads((app_dir / "data" / "pending.json").read_text())
    assert len(pending) == 1, "an accepted job was not recorded"
    record = next(iter(pending.values()))
    assert record["media"]["file_id"] == "FILE_A"
    assert record["placeholder_id"], "the status message id must be kept to resume editing"
    assert not telegram.documents, "the job should not have completed"


def test_a_pending_job_is_finished_after_a_restart(bot_factory, telegram, app_dir,
                                                   run_bot_until_done):
    """Telegram serves nothing on the second run; recovery is purely from disk."""
    first = bot_factory()
    first._connect()
    first.client.delete_webhook()
    telegram.queue_audio()
    threading.Thread(target=lambda: (time.sleep(1.5), first.stopping.set()), daemon=True).start()
    first._poll_loop()
    assert json.loads((app_dir / "data" / "pending.json").read_text())
    assert not telegram.documents

    # Restart. telegram has no updates left to serve.
    second = bot_factory()
    run_bot_until_done(second, telegram)

    assert telegram.documents, "the job was lost across the restart"
    assert json.loads((app_dir / "data" / "pending.json").read_text()) == {}


def test_the_resume_is_visible_to_the_user(bot_factory, telegram, app_dir, run_bot_until_done):
    first = bot_factory()
    first._connect()
    first.client.delete_webhook()
    telegram.queue_audio()
    threading.Thread(target=lambda: (time.sleep(1.5), first.stopping.set()), daemon=True).start()
    first._poll_loop()

    telegram.edits.clear()
    run_bot_until_done(bot_factory(), telegram)
    assert any("Resumed" in e["text"] for e in telegram.edits)


def test_unreadable_pending_records_are_dropped_not_crashed_on(bot_factory, app_dir):
    (app_dir / "data").mkdir(parents=True, exist_ok=True)
    (app_dir / "data" / "pending.json").write_text(json.dumps({
        "-1:1": {"chat_id": -1, "message_id": 1},          # no placeholder_id or media
    }))
    bot = bot_factory()
    bot._recover_pending()                                  # must not raise
    assert bot.jobs.qsize() == 0
    assert json.loads((app_dir / "data" / "pending.json").read_text()) == {}


def test_recovered_jobs_are_marked_resumed(app_dir):
    job = Job(-100, 55, 56, Media("F", "voice", 7, None, 10))
    assert job.resumed is False
    assert Job.from_record(job.to_record()).resumed is True
