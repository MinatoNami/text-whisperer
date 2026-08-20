"""Configuration, loaded from the process environment (populated from .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    base_url: str
    app_dir: Path
    state_path: Path
    pending_path: Path
    allowed_chat_ids: frozenset[int]
    whisper_model: str
    whisper_language: str | None
    whisper_initial_prompt: str | None
    max_audio_seconds: int
    delete_media_after: bool
    log_level: str
    archive_dir: Path
    keep_audio: bool
    show_timestamps: bool
    progress_interval: float
    web_enabled: bool
    web_host: str
    web_port: int
    llm_base_url: str
    llm_model: str
    llm_timeout: float
    auto_summarize_over_seconds: int
    skip_duplicates: bool
    # Long-poll window. Kept under launchd's 30s ExitTimeOut so a restart
    # doesn't have to wait for SIGKILL.
    poll_timeout: int = 25

    @property
    def api_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/bot{self.bot_token}"

    @property
    def file_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/file/bot{self.bot_token}"

    @staticmethod
    def from_env() -> "Config":
        token = _str("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
            )

        app_dir = Path(_str("APP_DIR") or Path(__file__).resolve().parents[2])
        raw_ids = _str("ALLOWED_CHAT_IDS")
        try:
            allowed = frozenset(
                int(part) for part in raw_ids.replace(" ", "").split(",") if part
            )
        except ValueError as exc:
            raise ConfigError(
                f"ALLOWED_CHAT_IDS must be comma-separated integers, got {raw_ids!r}"
            ) from exc

        return Config(
            bot_token=token,
            base_url=_str("BOT_API_BASE_URL", "http://127.0.0.1:8081"),
            app_dir=app_dir,
            state_path=app_dir / "data" / "state.json",
            pending_path=app_dir / "data" / "pending.json",
            allowed_chat_ids=allowed,
            whisper_model=_str("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo"),
            whisper_language=_str("WHISPER_LANGUAGE") or None,
            whisper_initial_prompt=_str("WHISPER_INITIAL_PROMPT") or None,
            max_audio_seconds=_int("MAX_AUDIO_SECONDS", 7200),
            delete_media_after=_bool("DELETE_MEDIA_AFTER", True),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
            archive_dir=Path(_str("ARCHIVE_DIR") or app_dir / "data" / "archive"),
            keep_audio=_bool("KEEP_AUDIO", True),
            show_timestamps=_bool("SHOW_TIMESTAMPS", True),
            progress_interval=_float("PROGRESS_INTERVAL", 4.0),
            web_enabled=_bool("WEB_ENABLED", True),
            # Loopback by default: the UI exposes transcripts of private
            # conversations with no authentication in front of them.
            web_host=_str("WEB_HOST", "127.0.0.1"),
            web_port=_int("WEB_PORT", 8090),
            # An OpenAI-compatible server: LM Studio, Ollama, llama.cpp, …
            llm_base_url=_str("LLM_BASE_URL", "http://127.0.0.1:1234"),
            llm_model=_str("LLM_MODEL"),
            llm_timeout=_float("LLM_TIMEOUT", 600.0),
            # Summarise longer recordings without being asked; short voice
            # notes are their own summary. 0 disables, -1 always summarises.
            auto_summarize_over_seconds=_int("AUTO_SUMMARIZE_OVER_SECONDS", 120),
            # Recognise a re-sent file and hand back the existing transcript
            # rather than spending the GPU on it again.
            skip_duplicates=_bool("SKIP_DUPLICATES", True),
        )


def load_dotenv(path: Path) -> None:
    """Minimal .env loader. Existing environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
