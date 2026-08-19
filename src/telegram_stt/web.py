"""Local monitoring UI: live queue plus a browsable, downloadable archive.

Runs as a thread inside the worker so it can read live job state directly
rather than guessing from disk. Bound to loopback by default — this serves
transcripts of private conversations and must not be reachable from the
network.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .llm import LLMError

log = logging.getLogger(__name__)

# Python's mimetypes maps .m4a to audio/mp4a-latm, which browsers refuse to
# play. Telegram's other containers need pinning too, so map them explicitly.
AUDIO_TYPES = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".m4b": "audio/mp4",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
}

UI_HTML = (Path(__file__).parent / "ui.html").read_bytes()


class _Handler(BaseHTTPRequestHandler):
    bot = None  # injected by serve()
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter than the default stderr spam
        log.debug("web: " + fmt, *args)

    # -- helpers -------------------------------------------------------------

    def _send(self, body: bytes, content_type: str, status=HTTPStatus.OK, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # No external references anywhere in the UI, so lock it right down.
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        # The page and its data change on every deploy and every job; a cached
        # copy shows stale UI or stale queue state.
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status=HTTPStatus.OK):
        self._send(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def _fail(self, status, message):
        self._json({"error": message}, status)

    def _record(self, stem: str):
        record = self.bot.archive.find(stem)
        if not record:
            self._fail(HTTPStatus.NOT_FOUND, f"no archived job {stem!r}")
            return None
        return record

    def _serve_file(self, path: Path, download_name: str, inline=False):
        data = path.read_bytes()
        guessed = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        disposition = "inline" if inline else "attachment"
        self._send(
            data,
            guessed,
            extra={"Content-Disposition": f'{disposition}; filename="{download_name}"'},
        )

    def _serve_media(self, path: Path):
        """Stream audio, honouring Range requests.

        Without 206 support a browser must download the whole file before it
        can play, and seeking does not work at all — which makes jumping to a
        timestamp in an hour-long recording useless.
        """
        size = path.stat().st_size
        content_type = AUDIO_TYPES.get(
            path.suffix.lower(), mimetypes.guess_type(path.name)[0] or "audio/mpeg"
        )
        start, end = 0, size - 1
        partial = False

        raw_range = self.headers.get("Range", "")
        match = re.match(r"bytes=(\d*)-(\d*)$", raw_range.strip()) if raw_range else None
        if match:
            first, last = match.group(1), match.group(2)
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            elif last:                      # a suffix range: the last N bytes
                start = max(0, size - int(last))
            if start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            partial = True

        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        # The page and its data change on every deploy and every job; a cached
        # copy shows stale UI or stale queue state.
        self.send_header("Cache-Control", "no-store")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                block = handle.read(min(64 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    # -- routes --------------------------------------------------------------

    def do_POST(self):
        route = unquote(urlparse(self.path).path)
        if not route.startswith("/api/summarize/"):
            return self._fail(HTTPStatus.NOT_FOUND, "no such route")

        record = self._record(route.rsplit("/", 1)[-1])
        if not record:
            return
        path = self.bot.archive.resolve(record.get("text_file"))
        if not path:
            return self._fail(HTTPStatus.GONE, "the transcript file is missing")

        stem = route.rsplit("/", 1)[-1]
        self.bot.start_summary(stem, record, path)
        return self._json({"started": True})

    def do_GET(self):
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        query = parse_qs(parsed.query)

        try:
            if route in ("/", "/index.html"):
                return self._send(UI_HTML, "text/html; charset=utf-8")

            if route == "/api/status":
                return self._json(self.bot.status())

            if route == "/api/history":
                limit = int((query.get("limit") or ["200"])[0])
                records = self.bot.archive.records()[::-1][:limit]
                for record in records:
                    record["id"] = Path(record.get("text_file", "")).stem
                    record["has_audio"] = bool(record.get("audio_file"))
                    record["has_summary"] = self.bot.archive.has_summary(record)
                    record["gist"] = self.bot.archive.summary_gist(record)
                return self._json(
                    {"records": records, "stats": self.bot.archive.stats(),
                     "disk": self.bot.archive.disk_usage()}
                )

            if route == "/api/search":
                q = (query.get("q") or [""])[0]
                return self._json({"query": q, "results": self.bot.archive.search(q)})

            if route.startswith("/api/summary-status/"):
                return self._json(self.bot.summary_status(route.rsplit("/", 1)[-1]))

            if route.startswith("/api/summary/"):
                record = self._record(route.rsplit("/", 1)[-1])
                if not record:
                    return
                summary = self.bot.archive.read_summary(record)
                if summary is None:
                    return self._fail(HTTPStatus.NOT_FOUND, "not summarised yet")
                return self._json({"summary": summary})

            if route.startswith("/api/transcript/"):
                record = self._record(route.rsplit("/", 1)[-1])
                if not record:
                    return
                path = self.bot.archive.resolve(record.get("text_file"))
                if not path:
                    return self._fail(HTTPStatus.GONE, "transcript file is missing")
                meta = self.bot.archive.resolve(record.get("meta_file"))
                segments = []
                if meta:
                    try:
                        stored = json.loads(meta.read_text(encoding="utf-8")).get("segments", [])
                    except ValueError:
                        stored = []
                    # Whisper emits empty segments; they would render as blank
                    # rows with a timestamp and nothing to click through to.
                    segments = [s for s in stored if (s.get("text") or "").strip()]
                return self._json({
                    "text": path.read_text(encoding="utf-8"),
                    "segments": segments,
                    "has_audio": bool(self.bot.archive.resolve(record.get("audio_file"))),
                    "audio_seconds": record.get("audio_seconds"),
                    "original_name": record.get("original_name"),
                })

            if route.startswith("/api/audio/"):
                record = self._record(route.rsplit("/", 1)[-1])
                if not record:
                    return
                path = self.bot.archive.resolve(record.get("audio_file"))
                if not path:
                    return self._fail(HTTPStatus.GONE, "no audio was archived for this job")
                return self._serve_media(path)

            if route.startswith("/api/download/"):
                _, _, _, kind, stem = route.split("/", 4)
                record = self._record(stem)
                if not record:
                    return
                if kind == "docx":
                    nice = Path(record.get("original_name") or stem).stem
                    with tempfile.TemporaryDirectory(prefix="stt-docx-") as tmp:
                        built = self.bot.archive.summary_docx(
                            record, Path(tmp) / f"{nice}-summary.docx"
                        )
                        if not built:
                            return self._fail(HTTPStatus.GONE, "not summarised yet")
                        return self._serve_file(built, built.name)

                if kind == "summary":
                    summary_path = self.bot.archive.summary_path(record)
                    if not summary_path or not summary_path.is_file():
                        return self._fail(HTTPStatus.GONE, "not summarised yet")
                    stem_name = Path(record.get("original_name") or stem).stem
                    return self._serve_file(summary_path, f"{stem_name}-summary.md")

                key = {"text": "text_file", "audio": "audio_file", "json": "meta_file"}.get(kind)
                if not key:
                    return self._fail(HTTPStatus.BAD_REQUEST, f"unknown kind {kind!r}")
                # Paths come from our own index and are re-checked against the
                # archive root, so a crafted stem cannot escape it.
                path = self.bot.archive.resolve(record.get(key))
                if not path:
                    return self._fail(HTTPStatus.GONE, f"no {kind} file for this job")
                nice = record.get("original_name") or stem
                name = Path(nice).stem + path.suffix
                return self._serve_file(path, name)

            return self._fail(HTTPStatus.NOT_FOUND, "no such route")
        except ConnectionError:
            # Browsers abort range requests constantly — every seek cancels the
            # request in flight. BrokenPipe, ConnectionReset and
            # ConnectionAborted all mean the same thing: the client left.
            pass
        except Exception as exc:
            log.exception("web request failed: %s", route)
            try:
                self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            except Exception:
                pass


class _QuietServer(ThreadingHTTPServer):
    """ThreadingHTTPServer prints a traceback when a client disconnects
    mid-response, which happens on every audio seek."""

    def handle_error(self, request, client_address):
        import sys

        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


def serve(bot, host: str, port: int) -> threading.Thread:
    """Start the UI on a daemon thread and return it."""
    handler = type("Handler", (_Handler,), {"bot": bot})
    server = _QuietServer((host, port), handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, name="web", daemon=True
    )
    thread.start()
    log.info("monitor UI on http://%s:%d", host, port)
    return thread
