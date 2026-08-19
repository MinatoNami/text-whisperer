#!/usr/bin/env bash
# Run the whole stack locally in the foreground: local Bot API server plus the
# worker. Ctrl-C stops both.
#
# NOTE: a bot token can only be polled from one place. Stop the M4 Pro first
#   ./scripts/deploy.sh --stop
# or point .env at a separate BotFather token for local work, otherwise the two
# pollers steal each other's updates.
#   ./scripts/dev.sh            start the stack (foreground)
#   ./scripts/dev.sh --status   where is it running, and is it healthy
#   ./scripts/dev.sh --stop     stop a stack started earlier
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

WEB_PORT_DEFAULT=8090
BOT_PORT_DEFAULT=8081

port_of() {  # port_of <VAR> <default>
  local v
  v="$(grep -E "^$1=" .env 2>/dev/null | cut -d= -f2- | tr -d ' ' || true)"
  echo "${v:-$2}"
}

case "${1:-}" in
  --status)
    WEB="$(port_of WEB_PORT $WEB_PORT_DEFAULT)"
    BOT="$(port_of BOT_API_PORT $BOT_PORT_DEFAULT)"
    echo "processes:"
    pgrep -fl "telegram-bot-api|python -m telegram_stt" 2>/dev/null | cut -c1-100 | sed 's/^/  /' \
      || echo "  (none running)"
    echo "listening:"
    lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -E ":($WEB|$BOT)\b" \
      | awk '{printf "  %-20s pid %-8s %s\n", $1, $2, $9}' || echo "  (nothing on $WEB/$BOT)"
    echo "monitor UI:"
    if curl -fsS --max-time 3 "http://127.0.0.1:$WEB/api/status" >/tmp/.stt-status 2>/dev/null; then
      echo "  http://127.0.0.1:$WEB  (open this in a browser)"
      python3 -c "
import json
d=json.load(open('/tmp/.stt-status'))
c=d.get('current') or {}
print(f\"  model    {d['model'].split('/')[-1]} ({'ready' if d['model_ready'] else 'loading'})\")
print(f\"  uptime   {d['uptime']:.0f}s, {d['completed_this_run']} job(s) this run\")
print(f\"  queue    {d['queue_depth']} waiting | now: {c.get('stage','idle')}\")
print(f\"  archive  {d['archive_dir']}\")"
    else
      echo "  not responding on port $WEB"
    fi
    echo "logs:"
    echo "  $APP_DIR/data/dev-bot-api.log   (bot api server)"
    echo "  worker logs go to this terminal when run in the foreground"
    exit 0
    ;;
  --stop)
    pkill -f "python -m telegram_stt" 2>/dev/null && echo "stopped worker" || echo "no worker running"
    pkill -f "vendor/telegram-bot-api/bin" 2>/dev/null && echo "stopped bot api" || echo "no bot api running"
    exit 0
    ;;
esac

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
