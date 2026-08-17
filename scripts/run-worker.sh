#!/usr/bin/env bash
# Launches the transcription worker under the project's uv-managed venv.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

# launchd hands us a bare PATH; Homebrew, uv and ffmpeg are not on it.
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

set -a
# shellcheck disable=SC1091
[[ -f .env ]] && source .env
set +a

export APP_DIR
# Keep model weights inside the project so a redeploy never re-downloads them.
export HF_HOME="${HF_HOME:-$APP_DIR/data/huggingface}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found on PATH. Install it with: brew install uv" >&2
  exit 1
fi

# launchd starts both services at once; give the Bot API server a moment to
# bind before we start hammering it. Any HTTP response (even 404) means it is up.
BASE_URL="${BOT_API_BASE_URL:-http://127.0.0.1:8081}"
for _ in $(seq 1 60); do
  curl -sS -o /dev/null --max-time 2 "$BASE_URL" 2>/dev/null && break
  sleep 1
done

exec uv run --no-sync python -m telegram_stt
