"""Local monitoring UI: live queue plus a browsable, downloadable archive.

Runs as a thread inside the worker so it can read live job state directly
rather than guessing from disk. Bound to loopback by default — this serves
transcripts of private conversations and must not be reachable from the
network.

Set WEB_PASSWORD and every route needs a login first, which is what makes it
safe to put a proxy (Tailscale Funnel, a tunnel, a reverse proxy) in front.
Leave it empty and the app is open, which is fine on loopback and nowhere else.
"""

from __future__ import annotations

import html
import json
import logging
import mimetypes
import re
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import auth
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
LOGIN_HTML = (Path(__file__).parent / "login.html").read_text(encoding="utf-8")

# Reachable without a session. Everything else needs one.
OPEN_ROUTES = {"/login", "/logout"}


class Gate:
    """The password check, or a no-op when no password is configured."""

    def __init__(self, config):
        self.password = getattr(config, "web_password", "")
        self.public = getattr(config, "web_public", False)
        self.days = getattr(config, "session_days", 30)
        self.throttle = auth.Throttle()
        self.secret = b""
        if self.password:
            self.secret = auth.load_secret(
                Path(config.app_dir) / "data" / "session.key"
            )
        elif getattr(config, "web_host", "127.0.0.1") not in ("127.0.0.1", "::1", "localhost"):
            # Bound off loopback with no password: reachable by anything that
            # can route to this machine. Worth shouting about.
            log.warning(
                "the web app is bound to %s with no WEB_PASSWORD — anything "
                "that can reach this host can read every transcript",
                config.web_host,
            )
        else:
            # Normal for a loopback or Tailscale-Serve setup, so not a warning.
            log.info("web app has no password; relying on it being loopback-only")

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def accepts(self, cookie: str | None) -> bool:
        return auth.verify(self.secret, self.password, cookie)


def gate_for(bot) -> Gate:
    """One gate per bot, built on first use so tests need no extra wiring."""
    existing = getattr(bot, "_web_gate", None)
    if existing is None:
        existing = Gate(bot.config)
        bot._web_gate = existing
    return existing


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
        # frame-ancestors stops the login form being framed and clickjacked
        # once the app is reachable from the internet.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        # Transcript ids live in the URL; don't leak them to anywhere the page
        # might link out to.
        self.send_header("Referrer-Policy", "no-referrer")
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

    # -- authentication ------------------------------------------------------

    @property
    def gate(self) -> Gate:
        return gate_for(self.bot)

    def _client(self) -> str:
        """Who to throttle.

        Behind Tailscale Funnel or any local reverse proxy every connection
        arrives from loopback, so the peer address would be one bucket for the
        whole internet. The proxy puts the real client in X-Forwarded-For; that
        header is only trustworthy because we bind to loopback, so the peer
        genuinely is the local proxy and not something that set it itself.
        """
        peer = self.client_address[0] if self.client_address else "?"
        if peer in ("127.0.0.1", "::1", "localhost"):
            forwarded = self.headers.get("X-Forwarded-For", "")
            first = forwarded.split(",")[0].strip()
            if first:
                return first[:64]
        return peer

    def _authorised(self, route: str) -> bool:
        """True to continue. Otherwise the response has already been sent."""
        if not self.gate.enabled or route in OPEN_ROUTES:
            return True
        if self.gate.accepts(auth.read_cookie(self.headers.get("Cookie"))):
            return True
        # The connection is kept alive, so an unread body would be parsed as
        # the next request line. Discard it before replying.
        pending = int(self.headers.get("Content-Length") or 0)
        while pending > 0:
            chunk = self.rfile.read(min(65536, pending))
            if not chunk:
                break
            pending -= len(chunk)

        if route.startswith("/api/"):
            # The UI reloads on a 401, which lands the browser on the form.
            self._fail(HTTPStatus.UNAUTHORIZED, "sign in first")
        else:
            self._send_login(next_path=self.path)
        return False

    @staticmethod
    def _safe_next(next_path: str) -> str:
        """Confine ?next= to a path on this app.

        Otherwise a crafted link sends someone through a real login and then
        straight to an attacker's page, which is where a convincing phish for
        the same password starts. Browsers read a leading `//` or `/\\` as
        protocol-relative, so both are off-site despite the leading slash.
        """
        if (not next_path.startswith("/")
                or next_path.startswith(("//", "/\\"))):
            return "/"
        return next_path

    def _send_login(self, next_path="/", error="", status=HTTPStatus.OK):
        next_path = self._safe_next(next_path)
        page = LOGIN_HTML.replace("__NEXT__", html.escape(next_path, quote=True))
        page = page.replace(
            "__ERROR__", f'<p class="bad">{html.escape(error)}</p>' if error else ""
        )
        self._send(page.encode("utf-8"), "text/html; charset=utf-8", status)

    def _redirect(self, location: str, cookies: list[str] | None = None):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        capped = min(length, 8192)          # a login form is never larger
        raw = self.rfile.read(capped).decode("utf-8", "replace")
        # Anything past the cap still has to leave the socket, or it is read
        # as the next request on this keep-alive connection.
        remaining = length - capped
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def _do_login(self):
        fields = self._form()
        next_path = self._safe_next(fields.get("next") or "/")
        client = self._client()

        delay = self.gate.throttle.delay_for(client)
        if delay:
            # Sleeping in the handler is the point: it makes guessing slow
            # without ever locking the real owner out.
            time.sleep(delay)

        if not auth.check_password(self.gate.password, fields.get("password", "")):
            self.gate.throttle.record_failure(client)
            log.warning("failed web login from %s", client)
            return self._send_login(
                next_path, "That password is not right.", HTTPStatus.UNAUTHORIZED
            )

        self.gate.throttle.clear(client)
        token, expires = auth.issue(self.gate.secret, self.gate.password, self.gate.days)
        log.info("web login from %s", client)
        return self._redirect(next_path, [
            auth.cookie_header(token, expires, self.gate.public),
            auth.hint_header(expires, self.gate.public),
        ])

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
        if not self._authorised(route):
            return

        if route == "/login":
            return self._do_login()

        if route == "/logout":
            return self._redirect("/", auth.clear_cookie_header(self.gate.public))


        # -- managing a recording ------------------------------------------
        if route.startswith("/api/record/"):
            parts = route.split("/")          # ['', 'api', 'record', <id>, <verb>]
            if len(parts) != 5:
                return self._fail(HTTPStatus.NOT_FOUND, "no such route")
            stem, verb = parts[3], parts[4]
            record = self._record(stem)
            if not record:
                return

            if verb == "update":
                size = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(size) or b"{}")
                except ValueError:
                    return self._fail(HTTPStatus.BAD_REQUEST, "body must be JSON")
                changes = {}
                if "title" in body:
                    changes["title"] = str(body["title"]).strip()[:120]
                if "tags" in body:
                    tags = body["tags"] if isinstance(body["tags"], list) else []
                    changes["tags"] = sorted({
                        str(t).strip().lower()[:30] for t in tags if str(t).strip()
                    })[:12]
                if "note" in body:
                    changes["note"] = str(body["note"])[:2000]
                if not changes:
                    return self._fail(HTTPStatus.BAD_REQUEST, "nothing to update")
                return self._json(self.bot.archive.set_meta(record, **changes).to_dict())

            if verb == "delete":
                return self._json(self.bot.archive.set_meta(record, deleted=True).to_dict())

            if verb == "restore":
                return self._json(self.bot.archive.set_meta(record, deleted=False).to_dict())

            if verb == "purge":
                # Irreversible, and separate from delete on purpose.
                return self._json({"removed": self.bot.archive.purge(record)})

            if verb == "describe":
                summary = self.bot.archive.read_summary(record)
                if not summary:
                    return self._fail(HTTPStatus.CONFLICT, "summarise it first")
                try:
                    described = self.bot.llm.describe(summary)
                except LLMError as exc:
                    return self._fail(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
                changes = {k: v for k, v in described.items() if v}
                if changes:
                    self.bot.archive.set_meta(record, **changes)
                return self._json(changes)

            return self._fail(HTTPStatus.NOT_FOUND, f"unknown action {verb!r}")

        if route == "/api/prune":
            days = self.bot.config.prune_audio_after_days
            removed, freed = self.bot.archive.prune_audio(days)
            return self._json({"removed": removed, "freed": freed, "days": days})

        if route == "/api/summarize-cancel-all":
            return self._json({"cancelled": self.bot.cancel_all_summaries()})

        if route.startswith("/api/summarize-cancel/"):
            stem = route.rsplit("/", 1)[-1]
            return self._json({"result": self.bot.cancel_summary(stem)})

        if route == "/api/summarize-batch":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._fail(HTTPStatus.BAD_REQUEST, "body must be JSON")
            ids = body.get("ids") or []
            if not isinstance(ids, list):
                return self._fail(HTTPStatus.BAD_REQUEST, "ids must be a list")
            force = bool(body.get("force"))

            queued, skipped, unknown = [], [], []
            for stem in ids[:200]:            # a sane ceiling on one request
                record = self.bot.archive.find(str(stem))
                if not record:
                    unknown.append(stem)
                    continue
                # Re-summarising something already done costs minutes for no
                # gain, so it is opt-in rather than the default.
                if not force and self.bot.archive.has_summary(record):
                    skipped.append(stem)
                    continue
                path = self.bot.archive.resolve(record.get("text_file"))
                if not path:
                    unknown.append(stem)
                    continue
                (queued if self.bot.start_summary(str(stem), record, path)
                 else skipped).append(stem)
            return self._json({
                "queued": len(queued), "skipped": len(skipped),
                "unknown": len(unknown), "ids": queued,
            })

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
            if route == "/login":
                if not self.gate.enabled:
                    return self._redirect("/")
                return self._send_login((query.get("next") or ["/"])[0])

            if not self._authorised(route):
                return

            if route in ("/", "/index.html"):
                return self._send(UI_HTML, "text/html; charset=utf-8")

            if route == "/api/status":
                return self._json(self.bot.status())

            if route == "/api/history":
                limit = int((query.get("limit") or ["200"])[0])
                deleted_only = (query.get("deleted") or ["0"])[0] == "1"
                records = self.bot.archive.records(include_deleted=deleted_only)
                if deleted_only:
                    records = [r for r in records if r.get("deleted")]
                records = records[::-1][:limit]
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

            if route == "/api/summary-queue":
                return self._json(self.bot.summary_overview())

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
    gate_for(bot)                        # built once, before any request
    handler = type("Handler", (_Handler,), {"bot": bot})
    server = _QuietServer((host, port), handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, name="web", daemon=True
    )
    thread.start()
    log.info("monitor UI on http://%s:%d", host, port)
    return thread
