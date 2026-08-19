import pytest

from telegram_stt.config import Config, ConfigError, load_dotenv


def test_missing_token_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("APP_DIR", str(tmp_path))
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        Config.from_env()


def test_defaults(app_dir):
    c = Config.from_env()
    assert c.base_url == "http://127.0.0.1:8081"
    assert c.whisper_model.endswith("whisper-large-v3-turbo")
    assert c.max_audio_seconds == 7200
    assert c.archive_dir == app_dir / "data" / "archive"
    assert c.pending_path == app_dir / "data" / "pending.json"
    assert c.web_host == "127.0.0.1", "the UI must not default to a routable address"


def test_api_urls_embed_the_token(app_dir):
    c = Config.from_env()
    assert c.api_url.endswith("/bot12345:test")
    assert c.file_url.endswith("/file/bot12345:test")


@pytest.mark.parametrize("raw,expected", [
    ("", frozenset()),
    ("42", frozenset({42})),
    ("1, -100200 ,3", frozenset({1, -100200, 3})),
])
def test_allowed_chat_ids_parsing(app_dir, monkeypatch, raw, expected):
    monkeypatch.setenv("ALLOWED_CHAT_IDS", raw)
    assert Config.from_env().allowed_chat_ids == expected


def test_malformed_chat_ids_are_rejected(app_dir, monkeypatch):
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "42,not-a-number")
    with pytest.raises(ConfigError, match="comma-separated integers"):
        Config.from_env()


def test_malformed_number_is_rejected(app_dir, monkeypatch):
    monkeypatch.setenv("MAX_AUDIO_SECONDS", "ten")
    with pytest.raises(ConfigError, match="must be an integer"):
        Config.from_env()


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("no", False),
])
def test_boolean_parsing(app_dir, monkeypatch, raw, expected):
    monkeypatch.setenv("SHOW_TIMESTAMPS", raw)
    assert Config.from_env().show_timestamps is expected


def test_dotenv_does_not_override_the_real_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("WHISPER_LANGUAGE=fr\nNEW_SETTING=from-file\n")
    monkeypatch.setenv("WHISPER_LANGUAGE", "en")
    monkeypatch.delenv("NEW_SETTING", raising=False)
    load_dotenv(env)
    import os
    assert os.environ["WHISPER_LANGUAGE"] == "en"   # env wins
    assert os.environ["NEW_SETTING"] == "from-file"  # file fills gaps


def test_dotenv_ignores_comments_and_blank_lines(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# a comment\n\nQUOTED=\"value\"\n")
    monkeypatch.delenv("QUOTED", raising=False)
    load_dotenv(env)
    import os
    assert os.environ["QUOTED"] == "value"
