"""The shipped .env.example must stay loadable by both readers.

The shell scripts `source` it while Python parses it, and the two disagree
about unquoted values containing spaces — bash tries to run the second word as
a command. That failure surfaces at service startup with a baffling message, so
guard it here instead.
"""

import re
import subprocess
from pathlib import Path

import pytest

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def assignments(text):
    for line in text.splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if m:
            yield m.group(1), m.group(2)


def test_example_file_exists():
    assert ENV_EXAMPLE.is_file()


def test_every_value_with_spaces_is_quoted():
    unquoted = [
        key for key, value in assignments(ENV_EXAMPLE.read_text())
        if value and re.search(r"\s", value) and not value.startswith(('"', "'"))
    ]
    assert not unquoted, f"these would break `source .env`: {unquoted}"


def test_bash_can_source_it(tmp_path):
    probe = tmp_path / "probe.sh"
    probe.write_text(f'set -a\n. "{ENV_EXAMPLE}"\nset +a\necho OK\n')
    result = subprocess.run(["bash", str(probe)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
    assert not result.stderr.strip(), f"sourcing produced noise: {result.stderr}"


def test_python_loader_agrees_with_bash(tmp_path, monkeypatch):
    from telegram_stt.config import load_dotenv

    sample = tmp_path / ".env"
    sample.write_text('PLAIN=value\nSPACED="two words"\nSINGLE=\'three word value\'\n')
    for key in ("PLAIN", "SPACED", "SINGLE"):
        monkeypatch.delenv(key, raising=False)
    load_dotenv(sample)
    import os
    assert os.environ["PLAIN"] == "value"
    assert os.environ["SPACED"] == "two words", "quotes must be stripped, not kept"
    assert os.environ["SINGLE"] == "three word value"


def test_no_real_credentials_are_committed():
    """.env.example is tracked; secrets must never be filled in."""
    for key, value in assignments(ENV_EXAMPLE.read_text()):
        if key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN"):
            assert value.strip().strip('"') == "", f"{key} has a value committed to git"


def test_web_host_defaults_to_loopback():
    values = dict(assignments(ENV_EXAMPLE.read_text()))
    assert values.get("WEB_HOST", "").strip('"') == "127.0.0.1"
