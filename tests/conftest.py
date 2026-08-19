"""Shared fixtures.

The fast suite never loads Whisper: transcription is stubbed so the bot's
control flow can be exercised in milliseconds. Tests that genuinely need the
model are marked `slow`.
"""

from __future__ import annotations

import json
import math
import struct
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    """An isolated APP_DIR with the minimum env the Config needs."""
    monkeypatch.setenv("APP_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "12345:test")
    monkeypatch.setenv("WEB_ENABLED", "0")
    monkeypatch.setenv("PROGRESS_INTERVAL", "0")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "")
    for leaked in ("ARCHIVE_DIR", "WHISPER_LANGUAGE", "WHISPER_INITIAL_PROMPT",
                   "MAX_AUDIO_SECONDS", "KEEP_AUDIO", "SHOW_TIMESTAMPS",
                   "DELETE_MEDIA_AFTER", "WHISPER_MODEL", "BOT_API_BASE_URL"):
        monkeypatch.delenv(leaked, raising=False)
    return tmp_path


@pytest.fixture
def tone_wav(tmp_path):
    """A real, decodable audio file — no speech, so it needs no model."""
    path = tmp_path / "tone.wav"
    rate, seconds = 16000, 2
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(rate * seconds)
        ))
    return path


SEGMENTS = (
    {"start": 0.0, "end": 4.8, "text": " Morning, did you look at the pipeline?"},
    {"start": 4.9, "end": 10.9, "text": " I did. The caching layer looks solid."},
    {"start": 11.0, "end": 13.8, "text": " That's fair."},
)


@pytest.fixture
def fake_transcribe(monkeypatch):
    """Replace Whisper with something instant and deterministic."""
    from telegram_stt import bot as bot_module
    from telegram_stt.transcribe import Transcript

    calls = []

    def stub(src, *, model, language=None, initial_prompt=None,
             max_seconds=0, on_progress=None, waveform=None):
        calls.append({"src": src, "model": model, "max_seconds": max_seconds})
        if on_progress:                      # drive the progress plumbing
            for f in (0.25, 0.5, 1.0):
                on_progress(f)
        return Transcript(
            text=" ".join(s["text"].strip() for s in SEGMENTS),
            language="en",
            audio_seconds=13.8,
            elapsed_seconds=0.2,
            segments=SEGMENTS,
        )

    monkeypatch.setattr(bot_module, "transcribe", stub)
    monkeypatch.setattr(bot_module, "warmup", lambda model: None)
    monkeypatch.setattr(bot_module, "decode_to_array", lambda p: ([0.0] * 16000, 1.0))
    return calls


class FakeTelegram:
    """A stand-in Bot API server behaving like one run with --local."""

    def __init__(self, media_path: Path):
        self.media_path = media_path
        self.updates: list[dict] = []
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.documents: list[bytes] = []
        self.deleted: list[int] = []
        self.delivered = threading.Event()
        self._served = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _reply(self, result):
                body = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                method = self.path.rsplit("/", 1)[-1]
                size = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(size) if size else b"{}"
                try:
                    params = json.loads(raw)
                except ValueError:
                    params = {}
                return outer.handle(self, method, params, raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def handle(self, req, method, params, raw):
        if method == "getMe":
            return req._reply({"id": 1, "username": "testbot", "is_bot": True})
        if method == "deleteWebhook":
            return req._reply(True)
        if method == "getUpdates":
            if self._served < len(self.updates):
                update = self.updates[self._served]
                self._served += 1
                return req._reply([update])
            time.sleep(0.05)
            return req._reply([])
        if method == "sendMessage":
            self.sent.append(params)
            return req._reply({"message_id": 900 + len(self.sent)})
        if method == "editMessageText":
            self.edits.append(params)
            return req._reply({"message_id": params["message_id"]})
        if method == "getFile":
            return req._reply({"file_path": str(self.media_path)})
        if method == "sendDocument":
            self.documents.append(raw)
            self.delivered.set()
            return req._reply({"message_id": 9999})
        if method == "deleteMessage":
            self.deleted.append(params.get("message_id"))
            return req._reply(True)
        return req._reply(True)

    def queue_audio(self, chat_id=-100999, message_id=55, **media):
        payload = {"file_id": "FILE_A", "duration": 14, "mime_type": "audio/ogg"}
        payload.update(media)
        self.updates.append({
            "update_id": 7000 + len(self.updates),
            "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "channel"},
                        "voice": payload},
        })

    def queue_text(self, text, chat_id=-100999, message_id=60):
        self.updates.append({
            "update_id": 7000 + len(self.updates),
            "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "channel"},
                        "text": text},
        })

    def stop(self):
        self._server.shutdown()


@pytest.fixture
def telegram(tone_wav):
    server = FakeTelegram(tone_wav)
    yield server
    server.stop()


@pytest.fixture
def run_bot_until_done():
    """Run the bot until a document is delivered, then stop it cleanly."""
    return _run_bot_until_done


def _run_bot_until_done(bot, telegram, timeout=20):
    def watchdog():
        telegram.delivered.wait(timeout=timeout)
        time.sleep(0.2)
        bot.stopping.set()

    threading.Thread(target=watchdog, daemon=True).start()
    worker = threading.Thread(target=bot._worker_loop, daemon=True)
    bot._connect()
    bot.client.delete_webhook()
    bot._recover_pending()
    worker.start()
    bot._poll_loop()
    bot.stopping.set()
    bot.jobs.put(None)
    worker.join(timeout=5)


@pytest.fixture
def server(app_dir, telegram, fake_transcribe, monkeypatch, run_bot_until_done):
    """A bot with one archived job, and the UI bound to an ephemeral port.

    Shared by the web and summary-flow suites.
    """
    from telegram_stt.bot import Bot
    from telegram_stt.config import Config

    monkeypatch.setenv("BOT_API_BASE_URL", telegram.base_url)
    bot = Bot(Config.from_env())
    telegram.queue_audio()
    _run_bot_until_done(bot, telegram)
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
