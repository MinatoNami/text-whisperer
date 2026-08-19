#!/usr/bin/env bash
# Run the whole stack locally in the foreground: local Bot API server plus the
# worker. Ctrl-C stops both.
#
# NOTE: a bot token can only be polled from one place. Stop the M4 Pro first
#   ./scripts/deploy.sh --stop
# or point .env at a separate BotFather token for local work, otherwise the two
# pollers steal each other's updates.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"
export HF_HOME="${HF_HOME:-$APP_DIR/data/huggingface}"

[[ -f .env ]] || { echo "no .env — copy .env.example and fill it in" >&2; exit 1; }

BIN="$APP_DIR/vendor/telegram-bot-api/bin/telegram-bot-api"
[[ -x "$BIN" ]] || { echo "telegram-bot-api not built. Run: ./scripts/build-bot-api.sh" >&2; exit 1; }

mkdir -p "$HOME/Library/Logs/telegram-stt"

cleanup() {
  [[ -n "${SERVER_PID:-}" ]] && kill "$SERVER_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "==> starting local Bot API server"
./scripts/run-bot-api.sh >"$APP_DIR/data/dev-bot-api.log" 2>&1 &
SERVER_PID=$!

BASE_URL="$(grep -E '^BOT_API_BASE_URL=' .env | cut -d= -f2- || true)"
BASE_URL="${BASE_URL:-http://127.0.0.1:8081}"
for _ in $(seq 1 60); do
  curl -sS -o /dev/null --max-time 2 "$BASE_URL" 2>/dev/null && break
  kill -0 "$SERVER_PID" 2>/dev/null || {
    echo "bot-api died on startup; see data/dev-bot-api.log" >&2
    tail -20 "$APP_DIR/data/dev-bot-api.log" >&2
    exit 1
  }
  sleep 1
done
echo "==> bot api listening at $BASE_URL  (log: data/dev-bot-api.log)"

echo "==> starting worker (ctrl-c to stop both)"
exec uv run python -m telegram_stt
