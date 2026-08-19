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
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .llm import LLMError

log = logging.getLogger(__name__)

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

        try:
            summary = self.bot.llm.summarise(path.read_text(encoding="utf-8"))
        except LLMError as exc:
            # The LLM being down is expected and actionable, not a server bug.
            return self._fail(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))

        try:
            self.bot.archive.write_summary(record, summary)
        except OSError as exc:
            log.warning("could not save summary: %s", exc)
        return self._json({"summary": summary})

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
                return self._json(
                    {"records": records, "stats": self.bot.archive.stats(),
                     "disk": self.bot.archive.disk_usage()}
                )

            if route == "/api/search":
                q = (query.get("q") or [""])[0]
                return self._json({"query": q, "results": self.bot.archive.search(q)})

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
                return self._json({"text": path.read_text(encoding="utf-8")})

            if route.startswith("/api/download/"):
                _, _, _, kind, stem = route.split("/", 4)
                record = self._record(stem)
                if not record:
                    return
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
        except BrokenPipeError:
            pass  # browser navigated away mid-response
        except Exception as exc:
            log.exception("web request failed: %s", route)
            try:
                self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            except Exception:
                pass


def serve(bot, host: str, port: int) -> threading.Thread:
    """Start the UI on a daemon thread and return it."""
    handler = type("Handler", (_Handler,), {"bot": bot})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, name="web", daemon=True
    )
    thread.start()
    log.info("monitor UI on http://%s:%d", host, port)
    return thread
