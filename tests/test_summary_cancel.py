"""Stopping summaries: queued ones vanish, running ones wind down."""

import json
import threading
import time
import urllib.request

import pytest


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


class Chunked:
    """Mimics a multi-part summary so cancellation has a boundary to land on."""

    def __init__(self, parts=6, delay=0.25):
        self.parts, self.delay = parts, delay
        self.completed_parts = 0
        self.finished = 0

    def summarise(self, text, on_progress=None, should_cancel=None):
        from telegram_stt.llm import LLMCancelled

        for i in range(1, self.parts + 1):
            if should_cancel and should_cancel():
                raise LLMCancelled("summary cancelled")
            if on_progress:
                on_progress(i / (self.parts + 1), f"part {i} of {self.parts}")
            time.sleep(self.delay)
            self.completed_parts += 1
        self.finished += 1
        return "## Summary\nDone."


@pytest.fixture
def many(server, tone_wav):
    base, bot = server
    for i in (2, 3, 4):
        bot.archive.store(
            chat_id=-1, message_id=i, source_audio=tone_wav,
            transcript_text=f"[00:00] recording {i}", media_kind="voice",
            original_name=f"meeting-{i}.m4a", language="en", model="m",
            audio_seconds=600.0, elapsed_seconds=1.0,
            segments=[{"start": 0.0, "end": 1.0, "text": f"recording {i}"}])
    ids = [r["id"] for r in get(base, "/api/history")["records"]]
    return base, bot, ids


def wait_for(fn, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.1)
    return False


class TestViewingTheQueue:
    def test_waiting_line_is_listed_in_order(self, many):
        base, bot, ids = many
        bot.llm = Chunked(parts=8, delay=0.3)
        post(base, "/api/summarize-batch", {"ids": ids})
        assert wait_for(lambda: get(base, "/api/summary-queue")["counts"]["running"] == 1)

        state = get(base, "/api/summary-queue")
        waiting = state["waiting"]
        assert waiting, "nothing reported as waiting"
        assert [w["position"] for w in waiting] == list(range(1, len(waiting) + 1))
        assert all(w["name"] for w in waiting), "queued items need a readable name"
        assert state["running"]["id"] not in [w["id"] for w in waiting]
        post(base, "/api/summarize-cancel-all")

    def test_overview_counts_every_state(self, many):
        base, bot, ids = many
        bot.llm = Chunked(parts=2, delay=0.1)
        post(base, "/api/summarize-batch", {"ids": ids})
        assert wait_for(lambda: get(base, "/api/summary-queue")["pending"] == 0, timeout=25)
        counts = get(base, "/api/summary-queue")["counts"]
        assert counts["done"] == len(ids)
        assert set(counts) == {"queued", "running", "done", "error", "cancelled"}


class TestCancelling:
    def test_a_queued_summary_is_removed_and_never_runs(self, many):
        base, bot, ids = many
        stub = Chunked(parts=10, delay=0.3)
        bot.llm = stub
        post(base, "/api/summarize-batch", {"ids": ids})
        assert wait_for(lambda: get(base, "/api/summary-queue")["counts"]["running"] == 1)

        queued = get(base, "/api/summary-queue")["waiting"]
        assert queued, "need something queued to cancel"
        victim = queued[-1]["id"]
        assert post(base, f"/api/summarize-cancel/{victim}")["result"] == "cancelled"

        post(base, "/api/summarize-cancel-all")
        assert wait_for(lambda: get(base, "/api/summary-queue")["pending"] == 0, timeout=25)
        assert stub.finished == 0, "a cancelled summary was still produced"

    def test_a_running_summary_stops_at_the_next_part(self, many):
        base, bot, ids = many
        stub = Chunked(parts=40, delay=0.15)   # long enough to interrupt
        bot.llm = stub
        post(base, "/api/summarize-batch", {"ids": ids[:1]})
        assert wait_for(lambda: get(base, "/api/summary-queue")["counts"]["running"] == 1)
        time.sleep(0.5)

        running = get(base, "/api/summary-queue")["running"]["id"]
        assert post(base, f"/api/summarize-cancel/{running}")["result"] == "stopping"

        assert wait_for(lambda: get(base, "/api/summary-queue")["pending"] == 0, timeout=25)
        assert stub.finished == 0, "it ran to completion despite being cancelled"
        assert stub.completed_parts < 40, "it did not stop early"

    def test_a_cancelled_summary_is_not_written_to_the_archive(self, many):
        base, bot, ids = many
        bot.llm = Chunked(parts=40, delay=0.15)
        post(base, "/api/summarize-batch", {"ids": ids[:1]})
        assert wait_for(lambda: get(base, "/api/summary-queue")["counts"]["running"] == 1)
        post(base, "/api/summarize-cancel-all")
        assert wait_for(lambda: get(base, "/api/summary-queue")["pending"] == 0, timeout=25)
        assert not bot.archive.has_summary(bot.archive.find(ids[0]))

    def test_cancel_all_clears_the_whole_queue(self, many):
        base, bot, ids = many
        bot.llm = Chunked(parts=40, delay=0.15)
        post(base, "/api/summarize-batch", {"ids": ids})
        assert wait_for(lambda: get(base, "/api/summary-queue")["counts"]["running"] == 1)
        assert post(base, "/api/summarize-cancel-all")["cancelled"] >= 1
        assert wait_for(lambda: get(base, "/api/summary-queue")["pending"] == 0, timeout=25)

    def test_cancelling_something_idle_is_harmless(self, many):
        base, _, ids = many
        assert post(base, f"/api/summarize-cancel/{ids[0]}")["result"] == "not running"
        assert post(base, "/api/summarize-cancel-all")["cancelled"] == 0

    def test_a_cancelled_recording_can_be_summarised_again(self, many):
        base, bot, ids = many
        bot.llm = Chunked(parts=40, delay=0.15)
        post(base, "/api/summarize-batch", {"ids": ids[:1]})
        assert wait_for(lambda: get(base, "/api/summary-queue")["counts"]["running"] == 1)
        post(base, "/api/summarize-cancel-all")
        assert wait_for(lambda: get(base, "/api/summary-queue")["pending"] == 0, timeout=25)

        bot.llm = Chunked(parts=2, delay=0.05)
        assert post(base, "/api/summarize-batch", {"ids": ids[:1]})["queued"] == 1
        assert wait_for(lambda: get(base, "/api/summary-queue")["pending"] == 0, timeout=25)
        # /api/history is newest-first while archive.records() is oldest-first,
        # so look the record up by id rather than by position.
        assert bot.archive.has_summary(bot.archive.find(ids[0]))
