"""Poll a Bot API server for audio, transcribe it locally, send the text back.

The poller and the transcriber are deliberately separate: transcription pins
the GPU for however long the recording is, and the poll loop has to keep
draining updates during that or Telegram starts backing them up.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .formatting import footer, human_duration, human_size
from .media import Media, extract_media
from .telegram import TelegramClient, TelegramError
from .transcribe import Transcript, TranscriptionError, transcribe, warmup

log = logging.getLogger(__name__)

QUEUE_SIZE = 32
CAPTION_LIMIT = 1024

HELP_TEXT = (
    "Send me a voice note, audio file, or video and I'll transcribe it with "
    "Whisper large-v3-turbo running locally on this Mac. Nothing leaves the "
    "machine.\n\n"
    "You'll get a receipt confirmation, a download confirmation, then the "
    "transcript as a .txt file.\n\n"
    "/status — model, queue depth, and this chat's ID\n"
    "/help — this message"
)


@dataclass
class Job:
    chat_id: int
    message_id: int
    placeholder_id: int
    media: Media


class Bot:
    def __init__(self, config: Config):
        self.config = config
        self.client = TelegramClient(
            config.api_url, config.file_url, poll_timeout=config.poll_timeout
        )
        self.jobs: queue.Queue[Job | None] = queue.Queue(maxsize=QUEUE_SIZE)
        self.stopping = threading.Event()
        self._model_ready = threading.Event()
        self._download_dir = Path(tempfile.gettempdir()) / "telegram-stt-downloads"

    # -- lifecycle -----------------------------------------------------------

    def _connect(self, deadline_seconds: int = 180) -> dict:
        """Wait for the Bot API server to be genuinely ready.

        The server binds its port before it has logged the bot in over MTProto,
        and answers `500 ... restart` in between. Without this the worker would
        die on boot and rely on launchd to restart it.
        """
        started = time.monotonic()
        delay = 1.0
        while True:
            try:
                return self.client.get_me()
            except TelegramError as exc:
                transient = exc.code is None or exc.code >= 500
                elapsed = time.monotonic() - started
                if not transient or elapsed > deadline_seconds:
                    raise
                log.info("bot api not ready yet (%s); retrying in %.0fs", exc, delay)
                if self.stopping.wait(delay):
                    raise
                delay = min(delay * 1.5, 10.0)

    def run(self) -> None:
        # Registered before connecting so a shutdown during the readiness wait
        # is still graceful.
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._on_signal)

        me = self._connect()
        log.info("connected as @%s via %s", me.get("username"), self.config.base_url)
        self.client.delete_webhook()

        worker = threading.Thread(target=self._worker_loop, name="transcriber", daemon=True)
        worker.start()

        try:
            self._poll_loop()
        finally:
            self.stopping.set()
            self.jobs.put(None)
            worker.join(timeout=30)
            self.client.close()
            log.info("stopped")

    def _on_signal(self, signum: int, _frame) -> None:
        log.info("received signal %s, shutting down", signum)
        self.stopping.set()

    # -- polling -------------------------------------------------------------

    def _load_offset(self) -> int | None:
        try:
            return json.loads(self.config.state_path.read_text())["offset"]
        except (OSError, ValueError, KeyError):
            return None

    def _save_offset(self, offset: int) -> None:
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"offset": offset}))
        tmp.replace(self.config.state_path)

    def _poll_loop(self) -> None:
        offset = self._load_offset()
        backoff = 1.0
        while not self.stopping.is_set():
            try:
                updates = self.client.get_updates(offset)
                backoff = 1.0
            except TelegramError as exc:
                log.warning("getUpdates failed: %s (retrying in %.0fs)", exc, backoff)
                self.stopping.wait(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self._handle_update(update)
                except Exception:
                    log.exception("failed to handle update %s", update.get("update_id"))
            if updates and offset is not None:
                self._save_offset(offset)

    # -- update dispatch -----------------------------------------------------

    def _allowed(self, chat_id: int) -> bool:
        return not self.config.allowed_chat_ids or chat_id in self.config.allowed_chat_ids

    def _handle_update(self, update: dict) -> None:
        message = update.get("message") or update.get("channel_post")
        if not message:
            return
        chat_id = message["chat"]["id"]
        message_id = message["message_id"]

        if not self._allowed(chat_id):
            log.warning("ignoring message from unauthorised chat %s", chat_id)
            return

        text = (message.get("text") or "").strip()
        if text.startswith("/"):
            self._handle_command(chat_id, message_id, text)
            return

        media = extract_media(message)
        if not media:
            return

        limit = self.config.max_audio_seconds
        if limit and media.duration and media.duration > limit:
            self.client.send_message(
                chat_id,
                f"That {media.label} is {human_duration(media.duration)} long; the "
                f"limit is {human_duration(limit)}.",
                reply_to=message_id,
            )
            return

        # Stage 1 of 3: acknowledge receipt straight away, before any work.
        detail = human_duration(media.duration) if media.duration else human_size(media.file_size)
        pending = self.jobs.qsize()
        queued = f" — {pending} job(s) ahead" if pending else ""
        placeholder = self.client.send_message(
            chat_id,
            f"📥 Received {media.label} ({detail}){queued}",
            reply_to=message_id,
        )

        try:
            self.jobs.put_nowait(
                Job(chat_id, message_id, placeholder["message_id"], media)
            )
        except queue.Full:
            self.client.edit_message(
                chat_id, placeholder["message_id"], "⚠️ Too many jobs queued — try again shortly."
            )

    def _handle_command(self, chat_id: int, message_id: int, text: str) -> None:
        command = text.split()[0].split("@")[0].lower()
        if command in ("/start", "/help"):
            self.client.send_message(chat_id, HELP_TEXT, reply_to=message_id)
        elif command == "/status":
            state = "ready" if self._model_ready.is_set() else "loading"
            self.client.send_message(
                chat_id,
                f"Model: {self.config.whisper_model} ({state})\n"
                f"Queue: {self.jobs.qsize()} waiting\n"
                f"Bot API: {self.config.base_url}\n"
                f"Chat ID: {chat_id}",
                reply_to=message_id,
            )

    # -- transcription worker ------------------------------------------------

    def _worker_loop(self) -> None:
        try:
            log.info("warming up %s", self.config.whisper_model)
            warmup(self.config.whisper_model)
            log.info("model ready")
        except Exception:
            # A failed warmup is not fatal — the real request will surface the
            # error to the user with context.
            log.exception("model warmup failed")
        self._model_ready.set()

        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                self._process(job)
            except Exception as exc:
                log.exception("job failed")
                self._report_failure(job, str(exc))
            finally:
                self.jobs.task_done()
            if self.stopping.is_set() and self.jobs.empty():
                return

    def _process(self, job: Job) -> None:
        started = time.monotonic()
        path = self.client.resolve_file(job.media.file_id, self._download_dir)

        # Stage 2 of 3: the bytes are on disk and about to hit the GPU.
        size = human_size(path.stat().st_size if path.is_file() else job.media.file_size)
        hint = human_duration(job.media.duration) if job.media.duration else "audio"
        self.client.edit_message(
            job.chat_id,
            job.placeholder_id,
            f"⬇️ Downloaded {size} — transcribing {hint}…",
        )

        try:
            result = transcribe(
                path,
                model=self.config.whisper_model,
                language=self.config.whisper_language,
                initial_prompt=self.config.whisper_initial_prompt,
                max_seconds=self.config.max_audio_seconds,
            )
        except TranscriptionError as exc:
            self._report_failure(job, str(exc))
            return
        finally:
            self._cleanup(path)

        log.info(
            "transcribed %s in %.1fs (%.1f× realtime, %d chars)",
            job.media.label,
            time.monotonic() - started,
            result.speed_factor,
            len(result.text),
        )
        self._deliver(job, result)

    def _cleanup(self, path: Path) -> None:
        # In --local mode the Bot API server keeps every download forever;
        # nobody else is going to clear these out.
        if not self.config.delete_media_after:
            return
        try:
            os.unlink(path)
        except OSError as exc:
            log.debug("could not remove %s: %s", path, exc)

    def _deliver(self, job: Job, result: Transcript) -> None:
        """Stage 3 of 3: upload the transcript as a .txt file."""
        tail = footer(
            self.config.whisper_model,
            result.language,
            result.audio_seconds,
            result.elapsed_seconds,
        )
        # Short transcripts also go in the caption so they are readable without
        # downloading. Telegram caps captions at 1024 characters.
        caption = tail
        if len(result.text) + len(tail) + 2 <= CAPTION_LIMIT:
            caption = f"{result.text}\n\n{tail}"

        stem = Path(job.media.file_name or f"transcript-{job.message_id}").stem
        with tempfile.TemporaryDirectory(prefix="telegram-stt-out-") as tmp:
            path = Path(tmp) / f"{stem}.txt"
            path.write_text(result.text, encoding="utf-8")
            self.client.send_document(job.chat_id, path, caption, reply_to=job.message_id)

        # The document and its caption carry everything the status message said.
        self.client.delete_message(job.chat_id, job.placeholder_id)

    def _report_failure(self, job: Job, detail: str) -> None:
        try:
            self.client.edit_message(
                job.chat_id, job.placeholder_id, f"⚠️ Transcription failed: {detail}"[:4000]
            )
        except TelegramError:
            log.exception("could not report failure to chat %s", job.chat_id)
