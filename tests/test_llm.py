"""Summarisation client. No real model is contacted."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from telegram_stt.llm import (
    LLMClient, LLMConfig, LLMError, split_for_context, strip_reasoning,
)


class FakeLLM:
    """A stand-in for LM Studio's OpenAI-compatible endpoint."""

    def __init__(self, reply="## Summary\nIt was a meeting.", models=("test-model",)):
        self.reply, self.models = reply, list(models)
        self.prompts: list[str] = []
        self.status = 200
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.endswith("/v1/models"):
                    self._send({"data": [{"id": m} for m in outer.models]})
                else:
                    self._send({}, 404)

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(size) or b"{}")
                outer.prompts.append(body["messages"][-1]["content"])
                if outer.status != 200:
                    return self._send({"error": "boom"}, outer.status)
                self._send({"choices": [{"message": {"content": outer.reply}}]})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self):
        self._server.shutdown()


@pytest.fixture
def fake_llm():
    server = FakeLLM()
    yield server
    server.stop()


@pytest.fixture
def client(fake_llm):
    c = LLMClient(LLMConfig(base_url=fake_llm.base_url, chunk_chars=200))
    yield c
    c.close()


class TestStripReasoning:
    def test_removes_a_think_block(self):
        assert strip_reasoning("<think>hmm, let me see</think>The answer.") == "The answer."

    def test_handles_other_tag_spellings(self):
        assert strip_reasoning("<reasoning>x</reasoning>Answer") == "Answer"

    def test_unterminated_block_keeps_only_what_follows(self):
        # A truncated model response must not dump its scratchpad on the user.
        assert strip_reasoning("<think>partial thought") == ""

    def test_leaves_ordinary_text_alone(self):
        assert strip_reasoning("## Summary\n- point") == "## Summary\n- point"


class TestSplitForContext:
    def test_short_text_is_one_chunk(self):
        assert split_for_context("hello", 100) == ["hello"]

    def test_splits_on_line_boundaries(self):
        text = "\n".join(f"[00:0{i}] line number {i}" for i in range(20))
        chunks = split_for_context(text, 100)
        assert len(chunks) > 1
        assert all(len(c) <= 120 for c in chunks)
        # no timestamped line may be cut in half
        for chunk in chunks:
            for line in chunk.splitlines():
                assert not line.startswith("0") or line.startswith("[")

    def test_nothing_is_lost(self):
        text = "\n".join(f"line {i}" for i in range(50))
        assert "".join(split_for_context(text, 60)).replace("\n", "") == text.replace("\n", "")

    def test_a_single_overlong_line_is_hard_split(self):
        chunks = split_for_context("x" * 500, 100)
        assert len(chunks) == 5 and all(len(c) == 100 for c in chunks)


class TestSummarise:
    def test_short_transcript_is_one_call(self, client, fake_llm):
        out = client.summarise("[00:00] a short meeting")
        assert out == "## Summary\nIt was a meeting."
        assert len(fake_llm.prompts) == 1

    def test_long_transcript_is_mapped_then_reduced(self, client, fake_llm):
        text = "\n".join(f"[00:{i:02d}] a line of meeting talk number {i}" for i in range(40))
        client.summarise(text)
        # several part-prompts, then one merge
        assert len(fake_llm.prompts) > 2
        assert any("part 1 of" in p for p in fake_llm.prompts)
        assert "Merge them into a single summary" in fake_llm.prompts[-1]

    def test_progress_is_reported_and_ends_at_one(self, client):
        text = "\n".join(f"[00:{i:02d}] line {i}" for i in range(40))
        seen = []
        client.summarise(text, on_progress=lambda f, label: seen.append(f))
        assert seen and seen[-1] == 1.0
        assert seen == sorted(seen)

    def test_reasoning_tags_are_stripped_from_the_result(self, fake_llm):
        fake_llm.reply = "<think>the user wants a summary</think>## Summary\nDone."
        c = LLMClient(LLMConfig(base_url=fake_llm.base_url))
        assert c.summarise("hello") == "## Summary\nDone."
        c.close()

    def test_empty_transcript_is_rejected(self, client):
        with pytest.raises(LLMError, match="nothing to summarise"):
            client.summarise("   ")

    def test_a_configured_model_overrides_discovery(self, fake_llm):
        c = LLMClient(LLMConfig(base_url=fake_llm.base_url, model="pinned-model"))
        assert c.available_model() == "pinned-model"
        c.close()

    def test_server_error_becomes_a_clear_message(self, client, fake_llm):
        fake_llm.status = 500
        with pytest.raises(LLMError, match="500"):
            client.summarise("hello")


def test_unreachable_server_explains_itself():
    c = LLMClient(LLMConfig(base_url="http://127.0.0.1:9", timeout=2))
    with pytest.raises(LLMError, match="Is LM Studio running"):
        c.summarise("hello")
    c.close()


def test_a_server_with_no_model_loaded_is_reported(fake_llm):
    fake_llm.models = []
    c = LLMClient(LLMConfig(base_url=fake_llm.base_url))
    with pytest.raises(LLMError, match="no model loaded"):
        c.summarise("hello")
    c.close()


def test_a_reply_that_is_only_reasoning_is_an_error(fake_llm):
    """Better to say the summary failed than to present scratchpad as one."""
    fake_llm.reply = "<think>I should summarise this but I ran out of room"
    c = LLMClient(LLMConfig(base_url=fake_llm.base_url))
    with pytest.raises(LLMError, match="only reasoning"):
        c.summarise("hello")
    c.close()
