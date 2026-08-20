"""Gist extraction and the asynchronous summarisation flow."""

import json
import time
import urllib.error
import urllib.request

import pytest

from telegram_stt.archive import Archive


@pytest.fixture
def archive(tmp_path, tone_wav):
    a = Archive(tmp_path / "archive")
    a.store(chat_id=-1, message_id=1, source_audio=tone_wav, transcript_text="[00:00] hello",
            media_kind="voice", original_name="meeting.m4a", language="en", model="m",
            audio_seconds=8.0, elapsed_seconds=1.0,
            segments=[{"start": 0.0, "end": 8.0, "text": "hello"}])
    return a


class TestGist:
    def test_first_prose_line_is_used(self, archive):
        record = archive.records()[0]
        archive.write_summary(record, "## Summary\nWe agreed to ship on Tuesday.\n\n## Key points\n- a")
        assert archive.summary_gist(record) == "We agreed to ship on Tuesday."

    def test_headings_and_bullets_are_skipped(self, archive):
        record = archive.records()[0]
        archive.write_summary(record, "# Title\n\n- bullet first\n\nActual prose here.")
        assert archive.summary_gist(record) == "Actual prose here."

    def test_bold_markers_are_stripped(self, archive):
        record = archive.records()[0]
        archive.write_summary(record, "## Summary\nA **bold** claim.")
        assert archive.summary_gist(record) == "A bold claim."

    def test_long_gist_is_truncated_with_an_ellipsis(self, archive):
        record = archive.records()[0]
        archive.write_summary(record, "## Summary\n" + "word " * 100)
        gist = archive.summary_gist(record, limit=60)
        assert len(gist) <= 60 and gist.endswith("…")

    def test_no_summary_gives_none(self, archive):
        assert archive.summary_gist(archive.records()[0]) is None


class TestAsyncSummarise:
    """A blocking POST left the browser with an indeterminate spinner for
    minutes even though the client reports which part it is on."""

    def _post(self, base, stem):
        request = urllib.request.Request(f"{base}/api/summarize/{stem}", method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def _status(self, base, stem):
        with urllib.request.urlopen(f"{base}/api/summary-status/{stem}", timeout=10) as r:
            return json.loads(r.read())

    def test_post_returns_immediately_and_status_reports_progress(self, server):
        base, bot = server
        seen = []

        class SlowStub:
            def summarise(self, text, on_progress=None, should_cancel=None):
                for i in (1, 2):
                    if on_progress:
                        on_progress(i / 3, f"part {i} of 2")
                    time.sleep(0.25)
                return "## Summary\nDone."

        bot.llm = SlowStub()
        stem = json.loads(urllib.request.urlopen(f"{base}/api/history").read())["records"][0]["id"]

        started = time.monotonic()
        assert self._post(base, stem)["started"] is True
        assert time.monotonic() - started < 2, "the POST should not block on the model"

        for _ in range(60):
            state = self._status(base, stem)
            seen.append((state["state"], state.get("label")))
            if state["state"] == "done":
                assert state["summary"] == "## Summary\nDone."
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"never finished: {seen}")

        labels = [label for _, label in seen if label]
        assert any("part" in (label or "") for label in labels), f"no per-part progress: {seen}"

    def test_the_summary_is_persisted_and_shows_up_as_a_gist(self, server):
        base, bot = server

        class Stub:
            def summarise(self, text, on_progress=None, should_cancel=None):
                return "## Summary\nA short meeting about nothing."

        bot.llm = Stub()
        stem = json.loads(urllib.request.urlopen(f"{base}/api/history").read())["records"][0]["id"]
        self._post(base, stem)
        for _ in range(60):
            if self._status(base, stem)["state"] == "done":
                break
            time.sleep(0.1)
        history = json.loads(urllib.request.urlopen(f"{base}/api/history").read())
        record = history["records"][0]
        assert record["has_summary"] is True
        assert record["gist"] == "A short meeting about nothing."

    def test_failure_is_reported_through_the_status_endpoint(self, server):
        base, bot = server

        class Boom:
            def summarise(self, text, on_progress=None, should_cancel=None):
                raise RuntimeError("model exploded")

        bot.llm = Boom()
        stem = json.loads(urllib.request.urlopen(f"{base}/api/history").read())["records"][0]["id"]
        self._post(base, stem)
        for _ in range(60):
            state = self._status(base, stem)
            if state["state"] == "error":
                assert "exploded" in state["error"]
                return
            time.sleep(0.1)
        pytest.fail("failure was never surfaced")

    def test_status_for_something_never_summarised_is_idle(self, server):
        base, _ = server
        assert self._status(base, "whatever")["state"] == "idle"

    def test_a_second_request_does_not_start_a_duplicate_run(self, server):
        base, bot = server
        calls = []

        class Counting:
            def summarise(self, text, on_progress=None, should_cancel=None):
                calls.append(1)
                time.sleep(0.6)
                return "## Summary\nOnce."

        bot.llm = Counting()
        stem = json.loads(urllib.request.urlopen(f"{base}/api/history").read())["records"][0]["id"]
        self._post(base, stem)
        self._post(base, stem)
        time.sleep(1.2)
        assert len(calls) == 1, "the same transcript was summarised twice concurrently"
