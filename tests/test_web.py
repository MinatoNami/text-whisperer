"""The monitor UI's HTTP surface, including the parts that serve files."""

import json
import urllib.error
import urllib.request

import pytest

from telegram_stt.bot import Bot
from telegram_stt.config import Config




def get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read(), dict(response.headers)


def get_json(url):
    status, body, _ = get(url)
    return status, json.loads(body)


def test_index_serves_the_ui(server):
    base, _ = server
    status, body, headers = get(f"{base}/")
    assert status == 200
    assert b"<title>Transcripts</title>" in body
    assert "text/html" in headers["Content-Type"]


def test_ui_declares_a_restrictive_csp(server):
    base, _ = server
    _, _, headers = get(f"{base}/")
    assert "default-src 'self'" in headers["Content-Security-Policy"]


def test_status_reports_the_live_shape(server):
    base, _ = server
    status, payload = get_json(f"{base}/api/status")
    assert status == 200
    for key in ("model", "model_ready", "queue_depth", "current", "waiting", "uptime"):
        assert key in payload
    assert payload["current"] is None, "nothing should be running once the job finished"


def test_history_lists_the_archived_job(server):
    base, _ = server
    _, payload = get_json(f"{base}/api/history")
    assert payload["stats"]["count"] == 1
    record = payload["records"][0]
    assert record["id"] and record["language"] == "en"
    assert record["has_audio"] is True


def test_transcript_endpoint_returns_the_text(server):
    base, _ = server
    _, history = get_json(f"{base}/api/history")
    stem = history["records"][0]["id"]
    _, payload = get_json(f"{base}/api/transcript/{stem}")
    assert "[00:00]" in payload["text"]


def test_downloads_are_attachments_with_a_useful_filename(server):
    base, _ = server
    _, history = get_json(f"{base}/api/history")
    stem = history["records"][0]["id"]
    for kind, expected in (("text", ".txt"), ("audio", ".wav"), ("json", ".json")):
        status, body, headers = get(f"{base}/api/download/{kind}/{stem}")
        assert status == 200 and body
        assert "attachment" in headers["Content-Disposition"]
        assert expected in headers["Content-Disposition"]


class TestDownloadsAreCaged:
    """The download route turns a URL segment into a file read. It must not be
    possible to reach anything outside the archive."""

    @pytest.mark.parametrize("stem", [
        "..%2f..%2f..%2f..%2fetc%2fpasswd",
        "..%2f..%2foutside.txt",
        "%2Fetc%2Fpasswd",
        "does-not-exist",
    ])
    def test_traversal_and_unknown_ids_are_refused(self, server, stem):
        base, _ = server
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(f"{base}/api/download/text/{stem}")
        assert exc.value.code == 404

    def test_an_unknown_file_kind_is_rejected(self, server):
        base, _ = server
        _, history = get_json(f"{base}/api/history")
        stem = history["records"][0]["id"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(f"{base}/api/download/secrets/{stem}")
        assert exc.value.code == 400


def test_unknown_routes_404(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(f"{base}/api/nope")
    assert exc.value.code == 404


class TestSearchEndpoint:
    def test_search_finds_the_archived_transcript(self, server):
        base, _ = server
        _, payload = get_json(f"{base}/api/search?q=caching")
        assert payload["results"], "the seeded transcript mentions caching"
        hit = payload["results"][0]
        assert hit["total_matches"] >= 1
        assert "start" in hit["matches"][0]

    def test_search_with_no_hits_is_an_empty_list_not_an_error(self, server):
        base, _ = server
        status, payload = get_json(f"{base}/api/search?q=kubernetes")
        assert status == 200 and payload["results"] == []

    def test_empty_query_returns_nothing(self, server):
        base, _ = server
        _, payload = get_json(f"{base}/api/search?q=")
        assert payload["results"] == []


class TestSummaryEndpoints:
    def test_history_flags_whether_a_summary_exists(self, server):
        base, bot = server
        _, payload = get_json(f"{base}/api/history")
        assert payload["records"][0]["has_summary"] is False
        bot.archive.write_summary(bot.archive.records()[0], "## Summary\nDone.")
        _, payload = get_json(f"{base}/api/history")
        assert payload["records"][0]["has_summary"] is True

    def test_reading_a_summary_that_does_not_exist_404s(self, server):
        base, _ = server
        _, history = get_json(f"{base}/api/history")
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(f"{base}/api/summary/{history['records'][0]['id']}")
        assert exc.value.code == 404

    def test_a_written_summary_can_be_read_back_and_downloaded(self, server):
        base, bot = server
        bot.archive.write_summary(bot.archive.records()[0], "## Summary\nIt happened.")
        _, history = get_json(f"{base}/api/history")
        stem = history["records"][0]["id"]

        _, payload = get_json(f"{base}/api/summary/{stem}")
        assert payload["summary"] == "## Summary\nIt happened."

        status, body, headers = get(f"{base}/api/download/summary/{stem}")
        assert status == 200 and b"It happened." in body
        assert "attachment" in headers["Content-Disposition"]
        assert "-summary.md" in headers["Content-Disposition"]

    def test_downloading_a_missing_summary_is_gone_not_a_crash(self, server):
        base, _ = server
        _, history = get_json(f"{base}/api/history")
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(f"{base}/api/download/summary/{history['records'][0]['id']}")
        assert exc.value.code == 410

    def test_a_dead_llm_surfaces_through_the_status_endpoint(self, server):
        """Summarisation runs on a thread now, so the POST cannot carry the
        error; it arrives via /api/summary-status instead."""
        import time

        base, bot = server
        from telegram_stt.llm import LLMClient, LLMConfig

        bot.llm = LLMClient(LLMConfig(base_url="http://127.0.0.1:9", timeout=2))
        _, history = get_json(f"{base}/api/history")
        stem = history["records"][0]["id"]
        request = urllib.request.Request(f"{base}/api/summarize/{stem}", method="POST")
        with urllib.request.urlopen(request, timeout=20) as response:
            assert json.loads(response.read())["started"] is True

        for _ in range(80):
            _, state = get_json(f"{base}/api/summary-status/{stem}")
            if state["state"] == "error":
                assert "LM Studio" in state["error"]
                return
            time.sleep(0.2)
        pytest.fail("the unreachable LLM was never reported")

    def test_summarize_writes_the_summary_to_the_archive(self, server):
        import time

        base, bot = server

        class Stub:
            def summarise(self, text, on_progress=None):
                return "## Summary\nStubbed."

        bot.llm = Stub()
        _, history = get_json(f"{base}/api/history")
        stem = history["records"][0]["id"]
        request = urllib.request.Request(f"{base}/api/summarize/{stem}", method="POST")
        urllib.request.urlopen(request, timeout=20).read()
        for _ in range(60):
            _, state = get_json(f"{base}/api/summary-status/{stem}")
            if state["state"] == "done":
                break
            time.sleep(0.1)
        assert bot.archive.read_summary(bot.archive.records()[0]) == "## Summary\nStubbed."

    def test_summarizing_an_unknown_id_404s(self, server):
        base, _ = server
        request = urllib.request.Request(f"{base}/api/summarize/nope", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=10)
        assert exc.value.code == 404

    def test_post_to_an_unknown_route_404s(self, server):
        base, _ = server
        request = urllib.request.Request(f"{base}/api/whatever", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=10)
        assert exc.value.code == 404


class TestAudioStreaming:
    """Seeking to a timestamp needs 206 responses; without them a browser has
    to fetch the whole recording before it can play anything."""

    def _stem(self, base):
        _, history = get_json(f"{base}/api/history")
        return history["records"][0]["id"]

    def test_full_request_advertises_range_support(self, server):
        base, _ = server
        status, body, headers = get(f"{base}/api/audio/{self._stem(base)}")
        assert status == 200
        assert headers["Accept-Ranges"] == "bytes"
        assert headers["Content-Type"].startswith("audio/")
        assert int(headers["Content-Length"]) == len(body)

    def test_a_byte_range_returns_206_with_content_range(self, server):
        base, _ = server
        request = urllib.request.Request(f"{base}/api/audio/{self._stem(base)}",
                                         headers={"Range": "bytes=0-99"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 206
            body = response.read()
            assert len(body) == 100
            assert response.headers["Content-Range"].startswith("bytes 0-99/")

    def test_a_mid_file_range_matches_the_file_on_disk(self, server):
        base, bot = server
        record = bot.archive.records()[0]
        on_disk = bot.archive.resolve(record["audio_file"]).read_bytes()
        request = urllib.request.Request(f"{base}/api/audio/{self._stem(base)}",
                                         headers={"Range": "bytes=500-599"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.read() == on_disk[500:600]

    def test_a_suffix_range_returns_the_tail(self, server):
        base, bot = server
        size = bot.archive.resolve(bot.archive.records()[0]["audio_file"]).stat().st_size
        request = urllib.request.Request(f"{base}/api/audio/{self._stem(base)}",
                                         headers={"Range": "bytes=-50"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 206
            assert len(response.read()) == 50
            assert response.headers["Content-Range"] == f"bytes {size-50}-{size-1}/{size}"

    def test_an_unsatisfiable_range_is_416_not_a_crash(self, server):
        base, _ = server
        request = urllib.request.Request(f"{base}/api/audio/{self._stem(base)}",
                                         headers={"Range": "bytes=99999999-"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=5)
        assert exc.value.code == 416

    def test_m4a_is_served_as_audio_mp4_so_browsers_will_play_it(self):
        """Python's mimetypes says audio/mp4a-latm, which browsers refuse."""
        from telegram_stt.web import AUDIO_TYPES
        assert AUDIO_TYPES[".m4a"] == "audio/mp4"
        assert AUDIO_TYPES[".ogg"] == "audio/ogg"

    def test_audio_for_an_unknown_id_404s(self, server):
        base, _ = server
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(f"{base}/api/audio/nope")
        assert exc.value.code == 404



class TestTranscriptPayload:
    def test_transcript_includes_segments_for_the_player(self, server):
        base, _ = server
        _, history = get_json(f"{base}/api/history")
        _, payload = get_json(f"{base}/api/transcript/{history['records'][0]['id']}")
        assert payload["segments"], "the viewer needs segments to make lines clickable"
        assert {"start", "end", "text"} <= set(payload["segments"][0])
        assert payload["has_audio"] is True

    def test_blank_segments_are_filtered_out(self, server, monkeypatch):
        """Whisper emits empty segments; they would render as blank rows."""
        base, bot = server
        record = bot.archive.records()[0]
        meta = bot.archive.resolve(record["meta_file"])
        meta.write_text(json.dumps({"segments": [
            {"start": 0.0, "end": 1.0, "text": "kept"},
            {"start": 1.0, "end": 1.0, "text": ""},
            {"start": 1.0, "end": 1.0, "text": "   "},
        ]}))
        _, payload = get_json(f"{base}/api/transcript/{record['text_file'].split('/')[-1][:-4]}")
        assert len(payload["segments"]) == 1
        assert payload["segments"][0]["text"] == "kept"
