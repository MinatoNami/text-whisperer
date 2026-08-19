"""Thin Bot API client. Works against a local telegram-bot-api server or the
cloud one — the only difference that matters here is how getFile behaves."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    def __init__(self, method: str, code: int | None, description: str):
        super().__init__(f"{method} failed ({code}): {description}")
        self.method = method
        self.code = code
        self.description = description


class TelegramClient:
    def __init__(self, api_url: str, file_url: str, poll_timeout: int = 50):
        self._api_url = api_url
        self._file_url = file_url
        self._poll_timeout = poll_timeout
        # Read timeout must outlive the long poll, or every getUpdates dies.
        self._http = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=poll_timeout + 20, write=60.0, pool=10.0)
        )

    def close(self) -> None:
        self._http.close()

    def call(self, method: str, **params: Any) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        try:
            response = self._http.post(f"{self._api_url}/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise TelegramError(method, None, str(exc)) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramError(method, response.status_code, response.text[:300]) from exc

        if not body.get("ok"):
            raise TelegramError(
                method, body.get("error_code"), body.get("description", "unknown error")
            )
        return body.get("result")

    # -- convenience wrappers ------------------------------------------------

    def get_me(self) -> dict:
        return self.call("getMe")

    def get_updates(self, offset: int | None) -> list[dict]:
        return self.call(
            "getUpdates",
            offset=offset,
            timeout=self._poll_timeout,
            allowed_updates=["message", "channel_post", "callback_query"],
        )

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        *,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        return self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            reply_to_message_id=reply_to,
            # Telegram errors the whole send if the replied-to message is gone.
            allow_sending_without_reply=True,
            disable_notification=True,
            link_preview_options={"is_disabled": True},
        )

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> None:
        try:
            self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            # Editing to identical text is a 400, not a real failure.
            if exc.code == 400 and "not modified" in exc.description.lower():
                return
            raise

    def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except TelegramError as exc:
            log.debug("deleteMessage ignored: %s", exc)

    def send_document(
        self,
        chat_id: int,
        path: Path,
        caption: str,
        reply_to: int | None = None,
        *,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        data = {
            "chat_id": str(chat_id),
            "caption": caption[:1024],
            "allow_sending_without_reply": "true",
            "disable_notification": "true",
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(reply_markup)
        if reply_to is not None:
            data["reply_to_message_id"] = str(reply_to)
        with path.open("rb") as handle:
            response = self._http.post(
                f"{self._api_url}/sendDocument",
                data=data,
                files={"document": (path.name, handle, "text/plain")},
            )
        body = response.json()
        if not body.get("ok"):
            raise TelegramError(
                "sendDocument", body.get("error_code"), body.get("description", "")
            )
        return body["result"]

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Acknowledge a button tap. Telegram shows a spinner until this lands."""
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text or None)
        except TelegramError as exc:
            log.debug("answerCallbackQuery ignored: %s", exc)

    def edit_reply_markup(self, chat_id: int, message_id: int, reply_markup: dict | None) -> None:
        try:
            self.call(
                "editMessageReplyMarkup",
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            log.debug("editMessageReplyMarkup ignored: %s", exc)

    def delete_webhook(self) -> None:
        """Polling and webhooks are mutually exclusive; make sure we own updates."""
        self.call("deleteWebhook", drop_pending_updates=False)

    def resolve_file(self, file_id: str, download_dir: Path) -> Path:
        """Return a local path to the media.

        A local Bot API server run with --local hands back an absolute path on
        this machine, so there is nothing to download. Against the cloud API we
        fall back to fetching the file over HTTP.
        """
        meta = self.call("getFile", file_id=file_id)
        file_path = meta.get("file_path")
        if not file_path:
            raise TelegramError("getFile", None, "response had no file_path")

        local = Path(file_path)
        if local.is_absolute() and local.is_file():
            return local

        download_dir.mkdir(parents=True, exist_ok=True)
        target = download_dir / Path(file_path).name
        with self._http.stream("GET", f"{self._file_url}/{file_path}") as response:
            response.raise_for_status()
            with target.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1 << 20):
                    handle.write(chunk)
        return target
