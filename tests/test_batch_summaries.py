"""Queuing several summaries at once."""

import json
import time
import urllib.error
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


class SlowStub:
    """Records call order so serialisation can be asserted."""

    def __init__(self, delay=0.3):
        self.delay = delay
        self.started, self.concurrent, self._live = [], 0, 0

    def summarise(self, text, on_progress=None):
        self._live += 1
        self.concurrent = max(self.concurrent, self._live)
        self.started.append(time.monotonic())
        if on_progress:
            on_progress(0.5, "part 1 of 2")
        time.sleep(self.delay)
        self._live -= 1
        return "## Summary\nDone."


@pytest.fixture
def many(server, tone_wav):
    """Three archived recordings, none summarised."""
    base, bot = server
    for i in (2, 3):
        bot.archive.store(
            chat_id=-1, message_id=i, source_audio=tone_wav,
            transcript_text=f"[00:00] recording {i}", media_kind="voice",
            original_name=f"meeting-{i}.m4a", language="en", model="m",
            audio_seconds=60.0, elapsed_seconds=1.0,
            segments=[{"start": 0.0, "end": 1.0, "text": f"recording {i}"}])
    ids = [r["id"] for r in get(base, "/api/history")["records"]]
    assert len(ids) == 3
    return base, bot, ids


def drain(base, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if get(base, "/api/summary-queue")["pending"] == 0:
            return True
        time.sleep(0.2)
    return False


class TestBatch:
    def test_queues_everything_selected(self, many):
        base, bot, ids = many
        bot.llm = SlowStub()
        result = post(base, "/api/summarize-batch", {"ids": ids})
        assert result["queued"] == 3 and result["skipped"] == 0
        assert drain(base), "the queue never drained"
        for record in bot.archive.records():
            assert bot.archive.has_summary(record)

    def test_summaries_run_one_at_a_time(self, many):
        """The LLM is a single resource; concurrent requests would thrash it."""
        base, bot, ids = many
        stub = SlowStub(delay=0.4)
        bot.llm = stub
        post(base, "/api/summarize-batch", {"ids": ids})
        assert drain(base)
        assert stub.concurrent == 1, f"ran {stub.concurrent} summaries at once"
        assert len(stub.started) == 3

    def test_already_summarised_are_skipped_not_redone(self, many):
        base, bot, ids = many
        bot.archive.write_summary(bot.archive.records()[0], "## Summary\nExisting.")
        stub = SlowStub()
        bot.llm = stub
        result = post(base, "/api/summarize-batch", {"ids": ids})
        assert result["skipped"] == 1
        assert result["queued"] == 2
        assert drain(base)
        assert len(stub.started) == 2, "a finished summary was recomputed"

    def test_force_redoes_them(self, many):
        base, bot, ids = many
        bot.archive.write_summary(bot.archive.records()[0], "## Summary\nOld.")
        bot.llm = SlowStub()
        result = post(base, "/api/summarize-batch", {"ids": ids, "force": True})
        assert result["queued"] == 3 and result["skipped"] == 0

    def test_queueing_the_same_id_twice_does_not_double_run(self, many):
        base, bot, ids = many
        stub = SlowStub(delay=0.5)
        bot.llm = stub
        post(base, "/api/summarize-batch", {"ids": ids})
        post(base, "/api/summarize-batch", {"ids": ids})   # immediately again
        assert drain(base)
        assert len(stub.started) == 3, f"ran {len(stub.started)} times for 3 recordings"

    def test_unknown_ids_are_counted_not_fatal(self, many):
        base, bot, ids = many
        bot.llm = SlowStub()
        result = post(base, "/api/summarize-batch", {"ids": ids[:1] + ["nope", "also-nope"]})
        assert result["queued"] == 1 and result["unknown"] == 2

    def test_an_empty_selection_is_harmless(self, many):
        base, _, _ = many
        assert post(base, "/api/summarize-batch", {"ids": []})["queued"] == 0

    def test_a_malformed_body_is_rejected(self, many):
        base, _, _ = many
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(base, "/api/summarize-batch", {"ids": "not-a-list"})
        assert exc.value.code == 400


class TestQueueReporting:
    def test_overview_reports_progress_while_running(self, many):
        base, bot, ids = many
        bot.llm = SlowStub(delay=0.8)
        post(base, "/api/summarize-batch", {"ids": ids})
        seen_running = False
        for _ in range(60):
            state = get(base, "/api/summary-queue")
            if state["counts"]["running"]:
                seen_running = True
                assert state["pending"] >= 1
                assert state["running"]["id"] in ids
            if state["pending"] == 0:
                break
            time.sleep(0.1)
        assert seen_running, "never observed a running summary"

    def test_per_record_state_lets_a_list_label_rows(self, many):
        base, bot, ids = many
        bot.llm = SlowStub(delay=0.6)
        post(base, "/api/summarize-batch", {"ids": ids})
        labelled = False
        for _ in range(60):
            per = get(base, "/api/summary-queue").get("per", {})
            if any(v["state"] in ("queued", "running") for v in per.values()):
                labelled = True
                break
            time.sleep(0.1)
        assert labelled, "no per-recording state was exposed"
        drain(base)

    def test_a_failure_is_reported_without_stalling_the_rest(self, many):
        base, bot, ids = many

        class Flaky:
            def __init__(self): self.calls = 0
            def summarise(self, text, on_progress=None):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("model exploded")
                return "## Summary\nFine."

        bot.llm = Flaky()
        post(base, "/api/summarize-batch", {"ids": ids})
        assert drain(base), "one failure stalled the queue"
        state = get(base, "/api/summary-queue")
        assert state["counts"]["error"] == 1
        assert state["counts"]["done"] == 2
