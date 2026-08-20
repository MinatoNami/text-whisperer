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

from .archive import Archive
from .config import Config
from .jobstore import JobStore
from .llm import LLMClient, LLMConfig
from .formatting import (
    eta,
    escape_html,
    human_size,
    human_duration,
    language_name,
    render_transcript,
    spoken_duration,
    summary_to_html,
)
from .media import Media, extract_media
from .telegram import TelegramClient, TelegramError
from .transcribe import (
    Transcript,
    TranscriptionError,
    decode_to_array,
    transcribe,
    warmup,
)

log = logging.getLogger(__name__)

QUEUE_SIZE = 32
CAPTION_LIMIT = 1024
SUMMARY_CALLBACK = "sum:"
# Telegram rejects a message over 4096 characters; longer summaries go as a file.
MESSAGE_LIMIT = 3800

HELP_TEXT = (
    "Send me a voice note, audio file, or video and I'll transcribe it with "
    "Whisper large-v3-turbo running locally on this Mac. Nothing leaves the "
    "machine.\n\n"
    "You'll get a receipt confirmation, live progress, then the transcript as "
    "a .txt file with timestamps.\n\n"
    "/status — model, queue depth, and this chat's ID\n"
    "/history — what's been transcribed and archived\n"
    "/help — this message"
)


@dataclass
class Job:
    chat_id: int
    message_id: int
    placeholder_id: int
    media: Media
    resumed: bool = False

    def to_record(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "placeholder_id": self.placeholder_id,
            "media": self.media.to_dict(),
        }

    @staticmethod
    def from_record(record: dict) -> "Job":
        return Job(
            chat_id=record["chat_id"],
            message_id=record["message_id"],
            placeholder_id=record["placeholder_id"],
            media=Media.from_dict(record["media"]),
            resumed=True,
        )


class ProgressReporter:
    """Edits one status message in place, no faster than `interval`.

    Telegram flood-limits edits, and a progress bar wants to tick far more
    often than it is safe to send. Everything is coalesced to the latest state;
    `force` bypasses the throttle for stage changes and the final update.
    """

    def __init__(self, client: TelegramClient, job: Job, interval: float, on_state=None):
        self._client = client
        self._job = job
        self._interval = interval
        self._last_sent = 0.0
        self._last_text = ""
        self._on_state = on_state
        self._started = time.monotonic()

    def show(self, text: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_sent < self._interval:
            return
        if text == self._last_text:
            return
        try:
            self._client.edit_message(self._job.chat_id, self._job.placeholder_id, text)
        except TelegramError as exc:
            log.debug("progress edit failed: %s", exc)
            return
        self._last_sent = now
        self._last_text = text

    def stage(self, label: str, fraction: float, *, force: bool = False) -> None:
        # The UI gets every tick; only the Telegram edit is rate-limited. Block
        # characters read as a glitch in a chat, so this is plain language with
        # a rough time remaining once there is enough signal to estimate one.
        if self._on_state:
            self._on_state(label, fraction)
        elapsed = time.monotonic() - self._started
        remaining = eta(fraction, elapsed)
        percent = f"{fraction * 100:.0f}%"
        tail = f" · {remaining}" if remaining else ""
        self.show(f"🎧 {label}… {percent}{tail}", force=force)


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
        self.archive = Archive(config.archive_dir, keep_audio=config.keep_audio)
        self.store = JobStore(config.pending_path)
        self.llm = LLMClient(LLMConfig(
            base_url=config.llm_base_url,
            model=config.llm_model,
            timeout=config.llm_timeout,
        ))
        self._state_lock = threading.Lock()
        self._summaries: dict[str, dict] = {}
        # Summaries run one at a time: the LLM is a single resource, and ten
        # concurrent requests would be slower than ten sequential ones as well
        # as risking the model's context being thrashed.
        self._summary_queue: queue.Queue = queue.Queue()
        self._summary_worker: threading.Thread | None = None
        self._current: dict | None = None
        self._started_at = time.time()
        self._completed = 0

    # -- live state for the web UI -------------------------------------------

    def _set_current(self, job: Job, stage: str, fraction: float) -> None:
        with self._state_lock:
            self._current = {
                "chat_id": job.chat_id,
                "message_id": job.message_id,
                "kind": job.media.kind,
                "label": job.media.label,
                "file_name": job.media.file_name,
                "duration": job.media.duration,
                "file_size": job.media.file_size,
                "stage": stage,
                "fraction": round(fraction, 4),
                "resumed": job.resumed,
                "started": self._current.get("started", time.time())
                if self._current and self._current.get("message_id") == job.message_id
                else time.time(),
            }

    def _clear_current(self) -> None:
        with self._state_lock:
            self._current = None

    def status(self) -> dict:
        with self._state_lock:
            current = dict(self._current) if self._current else None
        if current:
            current["elapsed"] = round(time.time() - current["started"], 1)
        waiting = [
            {
                "chat_id": r["chat_id"],
                "message_id": r["message_id"],
                "kind": r.get("media", {}).get("kind"),
                "file_name": r.get("media", {}).get("file_name"),
                "duration": r.get("media", {}).get("duration"),
            }
            for r in self.store.pending()
            if not current or r["message_id"] != current["message_id"]
        ]
        return {
            "model": self.config.whisper_model,
            "model_ready": self._model_ready.is_set(),
            "base_url": self.config.base_url,
            "archive_dir": str(self.config.archive_dir),
            "uptime": round(time.time() - self._started_at, 1),
            "completed_this_run": self._completed,
            "queue_depth": self.jobs.qsize(),
            "current": current,
            "waiting": waiting,
        }

    # -- summarisation -------------------------------------------------------

    def _ensure_summary_worker(self) -> None:
        """Start the summary worker on first use.

        Lazily rather than in run(), so anything holding a Bot — the web layer
        under test, for instance — gets a working queue without a poll loop.
        """
        with self._state_lock:
            if self._summary_worker and self._summary_worker.is_alive():
                return
            self._summary_worker = threading.Thread(
                target=self._summary_loop, name="summaries", daemon=True
            )
            self._summary_worker.start()

    def _summary_loop(self) -> None:
        while True:
            item = self._summary_queue.get()
            if item is None:
                return
            stem, record, transcript_path = item
            try:
                self._run_summary(stem, record, transcript_path)
            except Exception as exc:
                log.warning("summary worker failed for %s: %s", stem, exc)
                with self._state_lock:
                    self._summaries[stem] = {
                        "state": "error", "fraction": 0.0, "label": "failed",
                        "error": str(exc),
                    }
            finally:
                self._summary_queue.task_done()

    def _run_summary(self, stem: str, record: dict, transcript_path: Path) -> None:
        with self._state_lock:
            self._summaries[stem] = {
                "state": "running", "fraction": 0.0, "label": "starting",
            }

        def progress(fraction: float, label: str) -> None:
            with self._state_lock:
                entry = self._summaries.get(stem)
                if entry is not None:
                    entry["fraction"] = round(fraction, 3)
                    entry["label"] = label

        summary = self.llm.summarise(
            transcript_path.read_text(encoding="utf-8"), on_progress=progress
        )
        try:
            self.archive.write_summary(record, summary)
        except OSError as exc:
            log.warning("could not save summary for %s: %s", stem, exc)
        with self._state_lock:
            self._summaries[stem] = {
                "state": "done", "fraction": 1.0, "label": "done", "summary": summary,
            }

    def start_summary(self, stem: str, record: dict, transcript_path: Path) -> bool:
        """Queue a summary. Returns False if one is already queued or running."""
        with self._state_lock:
            existing = self._summaries.get(stem)
            if existing and existing.get("state") in ("queued", "running"):
                return False
            self._summaries[stem] = {
                "state": "queued", "fraction": 0.0, "label": "waiting",
            }
        self._summary_queue.put((stem, record, transcript_path))
        self._ensure_summary_worker()
        return True

    def summary_overview(self) -> dict:
        """Queue-wide state, for a UI running a batch."""
        with self._state_lock:
            entries = {k: dict(v) for k, v in self._summaries.items()}
        counts = {"queued": 0, "running": 0, "done": 0, "error": 0}
        running = None
        for stem, entry in entries.items():
            state = entry.get("state", "idle")
            if state in counts:
                counts[state] += 1
            if state == "running":
                running = {"id": stem, **{k: entry.get(k) for k in ("fraction", "label")}}
        return {
            "counts": counts,
            "running": running,
            "pending": counts["queued"] + counts["running"],
            # Per-recording state, so a list can label each row directly.
            "per": {
                stem: {k: entry.get(k) for k in ("state", "fraction", "label")}
                for stem, entry in entries.items()
                if entry.get("state") in ("queued", "running", "error")
            },
        }

    def summary_status(self, stem: str) -> dict:
        with self._state_lock:
            entry = self._summaries.get(stem)
            return dict(entry) if entry else {"state": "idle"}

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
        self._recover_pending()
        self._start_web()

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

    def _start_web(self) -> None:
        if not self.config.web_enabled:
            return
        try:
            from .web import serve

            serve(self, self.config.web_host, self.config.web_port)
        except OSError as exc:
            # A busy port must not stop transcription from running.
            log.warning(
                "monitor UI not started on %s:%s (%s)",
                self.config.web_host, self.config.web_port, exc,
            )

    def _recover_pending(self) -> None:
        """Re-queue jobs that were accepted but never finished."""
        pending = self.store.pending()
        if not pending:
            return
        log.info("resuming %d unfinished job(s) from a previous run", len(pending))
        for record in pending:
            try:
                job = Job.from_record(record)
            except (KeyError, TypeError) as exc:
                log.warning("dropping unreadable pending job %r: %s", record, exc)
                self.store.remove(
                    record.get("chat_id", 0), record.get("message_id", 0)
                )
                continue
            try:
                self.jobs.put_nowait(job)
            except queue.Full:
                log.warning("queue full while resuming; %s stays pending", job.message_id)

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
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return
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

        # Acknowledge immediately, in the words a person would use.
        length = spoken_duration(media.duration)
        pending = self.jobs.qsize()
        if pending:
            note = f"in the queue behind {pending} other{'s' if pending > 1 else ''}"
        else:
            note = "starting now"
        opening = f"🎧 Got your {length} recording — {note}…" if length \
            else f"🎧 Got your {media.label} — {note}…"
        placeholder = self.client.send_message(chat_id, opening, reply_to=message_id)

        job = Job(chat_id, message_id, placeholder["message_id"], media)
        # Recorded before queueing: if the process dies with this job still in
        # the queue, Telegram will not resend it (the offset has moved on), so
        # the on-disk record is the only way back to it.
        self.store.add(job.to_record())
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            self.store.remove(chat_id, message_id)
            self.client.edit_message(
                chat_id,
                placeholder["message_id"],
                "😕 I have too much queued up right now — send that again in a few minutes.",
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
                f"Timestamps: {'on' if self.config.show_timestamps else 'off'}\n"
                f"Queue: {self.jobs.qsize()} waiting\n"
                f"Bot API: {self.config.base_url}\n"
                f"Chat ID: {chat_id}",
                reply_to=message_id,
            )
        elif command == "/history":
            self.client.send_message(chat_id, self._history_summary(), reply_to=message_id)

    def _history_summary(self) -> str:
        try:
            stats = self.archive.stats()
            recent = self.archive.recent(5)
            usage = self.archive.disk_usage()
        except OSError as exc:
            return f"Could not read the archive: {exc}"

        if not stats["count"]:
            return f"No transcriptions archived yet.\nArchive: {self.config.archive_dir}"

        lines = [
            f"{stats['count']} transcription(s) archived",
            f"{human_duration(stats['audio_seconds'])} of audio · "
            f"{stats['characters']:,} characters · {human_size(usage)} on disk",
            f"Archive: {self.config.archive_dir}",
            "",
            "Recent:",
        ]
        for record in recent:
            when = (record.get("timestamp") or "")[:16].replace("T", " ")
            lines.append(
                f"  {when} · {human_duration(record.get('audio_seconds') or 0)}"
                f" · {record.get('original_name') or record.get('media_kind')}"
            )
        return "\n".join(lines)

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
                # Terminal either way — a job that failed should not be retried
                # forever on every restart.
                self.store.remove(job.chat_id, job.message_id)
                self._clear_current()
                self._completed += 1
                self.jobs.task_done()
            if self.stopping.is_set() and self.jobs.empty():
                return

    def _process(self, job: Job) -> None:
        started = time.monotonic()
        path = self.client.resolve_file(job.media.file_id, self._download_dir)

        # Stage 2 of 3: the bytes are on disk and about to hit the GPU.
        hint = spoken_duration(job.media.duration) if job.media.duration else "audio"
        reporter = ProgressReporter(
            self.client,
            job,
            self.config.progress_interval,
            on_state=lambda stage, fraction: self._set_current(job, stage, fraction),
        )
        self._set_current(job, "Preparing", 0.0)
        prefix = "🔄 Picking this back up — " if job.resumed else ""
        reporter.show(f"{prefix}🎧 Getting your {hint} recording ready…", force=True)

        try:
            # Decoded once here and handed to both engines; each would
            # otherwise shell out to its own ffmpeg.
            waveform, _ = decode_to_array(path)

            reporter.stage("Transcribing", 0.0, force=True)
            result = transcribe(
                path,
                waveform=waveform,
                model=self.config.whisper_model,
                language=self.config.whisper_language,
                initial_prompt=self.config.whisper_initial_prompt,
                max_seconds=self.config.max_audio_seconds,
                on_progress=lambda f: reporter.stage("Transcribing", f),
            )

            log.info(
                "transcribed %s in %.1fs (%.1f× realtime, %d chars)",
                job.media.label,
                time.monotonic() - started,
                result.speed_factor,
                len(result.text),
            )
            # Delivered inside the try so the archive can still copy the audio;
            # the finally below removes the Bot API server's own copy.
            self._deliver(job, result, path)
        except TranscriptionError as exc:
            self._report_failure(job, str(exc))
        finally:
            self._cleanup(path)

    def _cleanup(self, path: Path) -> None:
        # In --local mode the Bot API server keeps every download forever;
        # nobody else is going to clear these out.
        if not self.config.delete_media_after:
            return
        try:
            os.unlink(path)
        except OSError as exc:
            log.debug("could not remove %s: %s", path, exc)

    def _deliver(self, job: Job, result: Transcript, source_audio: Path) -> None:
        """Stage 3 of 3: archive everything, then upload the transcript."""
        body = render_transcript(
            result.segments, with_timestamps=self.config.show_timestamps
        )
        # Whisper occasionally returns text with no segmentation; the flat
        # string is all we have then.
        if not body:
            body = result.text

        spoken = spoken_duration(result.audio_seconds)
        language = language_name(result.language)
        tail = " · ".join(x for x in (f"📝 {spoken} recording" if spoken else "📝 Transcript",
                                      language) if x)

        entry_stem = None
        try:
            entry = self.archive.store(
                chat_id=job.chat_id,
                message_id=job.message_id,
                source_audio=source_audio,
                transcript_text=body,
                media_kind=job.media.kind,
                original_name=job.media.file_name,
                language=result.language,
                model=self.config.whisper_model,
                audio_seconds=result.audio_seconds,
                elapsed_seconds=result.elapsed_seconds,
                segments=result.segments,
            )
            entry_stem = entry.stem
        except OSError as exc:
            # Never lose the transcript to an archiving problem.
            log.warning("could not archive job %s: %s", job.message_id, exc)

        # Short transcripts also go in the caption so they are readable without
        # downloading. Telegram caps captions at 1024 characters.
        caption = tail
        if len(body) + len(tail) + 2 <= CAPTION_LIMIT:
            caption = f"{body}\n\n{tail}"

        stem = Path(job.media.file_name or f"transcript-{job.message_id}").stem
        with tempfile.TemporaryDirectory(prefix="telegram-stt-out-") as tmp:
            path = Path(tmp) / f"{stem}.txt"
            # The file holds the transcript and nothing else; run metadata
            # lives in the caption and the archive index.
            path.write_text(body, encoding="utf-8")
            sent = self.client.send_document(
                job.chat_id,
                path,
                caption,
                reply_to=job.message_id,
                reply_markup=self._summary_button(entry_stem),
            )

        # The document and its caption carry everything the status message said.
        self.client.delete_message(job.chat_id, job.placeholder_id)

        limit = self.config.auto_summarize_over_seconds
        wanted = limit == -1 or (limit > 0 and result.audio_seconds >= limit)
        if wanted and entry_stem:
            self._deliver_summary(job.chat_id, entry_stem, sent.get("message_id"))

    @staticmethod
    def _summary_button(stem: str | None) -> dict | None:
        if not stem:
            return None
        return {"inline_keyboard": [[
            {"text": "✨ Summarise this", "callback_data": f"{SUMMARY_CALLBACK}{stem}"}
        ]]}

    def _deliver_summary(self, chat_id: int, stem: str, reply_to: int | None) -> None:
        """Summarise and post it, editing one status message as it goes."""
        record = self.archive.find(stem)
        if not record:
            return
        transcript = self.archive.resolve(record.get("text_file"))
        if not transcript:
            return

        cached = self.archive.read_summary(record)
        status = None
        if cached is None:
            status = self.client.send_message(
                chat_id, "✨ Reading it back and writing a summary…", reply_to=reply_to
            )
            try:
                cached = self.llm.summarise(
                    transcript.read_text(encoding="utf-8"),
                    on_progress=lambda f, label: self.client.edit_message(
                        chat_id, status["message_id"],
                        f"✨ Writing a summary… {label}",
                    ) if label else None,
                )
                self.archive.write_summary(record, cached)
            except Exception as exc:
                log.warning("summary for %s failed: %s", stem, exc)
                self.client.edit_message(
                    chat_id, status["message_id"],
                    f"😕 I couldn't write a summary.\n\n{exc}"[:4000],
                )
                return

        body = summary_to_html(cached)
        # The model's own first section is usually called "Summary"; adding
        # another heading above it just repeats the word.
        header = "" if body.lower().startswith("<b>summary</b>") else "<b>✨ Summary</b>\n\n"
        readable = len(body) + len(header) <= MESSAGE_LIMIT

        # Post the text when it fits, so the summary can be read in the chat
        # without downloading anything.
        if readable:
            text = header + body
            if status:
                self.client.edit_message(
                    chat_id, status["message_id"], text, parse_mode="HTML"
                )
            else:
                self.client.send_message(
                    chat_id, text, reply_to=reply_to, parse_mode="HTML"
                )

        # The Word document goes with every summary either way — it is what
        # actually gets forwarded on.
        self._send_summary_document(chat_id, record, stem, reply_to, brief=readable)

        if status and not readable:
            self.client.delete_message(chat_id, status["message_id"])

    def _send_summary_document(
        self, chat_id: int, record: dict, stem: str, reply_to: int | None, *, brief: bool
    ) -> None:
        """Attach the summary as .docx, falling back to Markdown if it fails."""
        name = Path(record.get("original_name") or stem).stem
        caption = "📄 Also as a Word document" if brief else "✨ Summary"
        with tempfile.TemporaryDirectory(prefix="telegram-stt-sum-") as tmp:
            target = Path(tmp) / f"{name}-summary.docx"
            try:
                built = self.archive.summary_docx(record, target)
            except Exception as exc:
                # A rendering problem must not swallow the summary itself.
                log.warning("could not build a docx for %s: %s", stem, exc)
                built = None
            if built is None:
                built = Path(tmp) / f"{name}-summary.md"
                built.write_text(self.archive.read_summary(record) or "", encoding="utf-8")
            try:
                self.client.send_document(chat_id, built, caption, reply_to=reply_to)
            except TelegramError as exc:
                log.warning("could not send the summary document: %s", exc)

    def _handle_callback(self, callback: dict) -> None:
        data = callback.get("data") or ""
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if not data.startswith(SUMMARY_CALLBACK) or chat_id is None:
            self.client.answer_callback(callback["id"])
            return
        if not self._allowed(chat_id):
            self.client.answer_callback(callback["id"], "Not allowed here.")
            return

        stem = data[len(SUMMARY_CALLBACK):]
        self.client.answer_callback(callback["id"], "Working on it…")
        # Drop the button so it cannot be tapped twice.
        self.client.edit_reply_markup(chat_id, message.get("message_id"), None)
        threading.Thread(
            target=self._deliver_summary,
            args=(chat_id, stem, message.get("message_id")),
            name=f"summary-{stem}",
            daemon=True,
        ).start()

    def _report_failure(self, job: Job, detail: str) -> None:
        try:
            self.client.edit_message(
                job.chat_id,
                job.placeholder_id,
                f"😕 I couldn't transcribe that one.\n\n{detail}"[:4000],
            )
        except TelegramError:
            log.exception("could not report failure to chat %s", job.chat_id)
