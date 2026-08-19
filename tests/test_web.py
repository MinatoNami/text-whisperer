"""The monitor UI's HTTP surface, including the parts that serve files."""

import json
import urllib.error
import urllib.request

import pytest

from telegram_stt.bot import Bot
from telegram_stt.config import Config


@pytest.fixture
def server(app_dir, telegram, fake_transcribe, monkeypatch, run_bot_until_done):
    """A bot with one archived job, and the UI bound to an ephemeral port."""
    monkeypatch.setenv("BOT_API_BASE_URL", telegram.base_url)
    bot = Bot(Config.from_env())
    telegram.queue_audio()
    run_bot_until_done(bot, telegram)
    # Bind directly rather than via serve(), so the test owns the socket and
    # can shut it down; serve() keeps its server private.
    import threading
    from http.server import ThreadingHTTPServer

    from telegram_stt.web import _Handler

    handler = type("H", (_Handler,), {"bot": bot})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", bot
    httpd.shutdown()


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
    assert b"<title>telegram-stt monitor</title>" in body
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
