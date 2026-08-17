#!/usr/bin/env bash
# Launches the local tdlib Bot API server. Secrets are read from .env rather
# than passed as flags so they never show up in `ps`.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

set -a
# shellcheck disable=SC1091
[[ -f .env ]] && source .env
set +a

: "${TELEGRAM_API_ID:?TELEGRAM_API_ID is not set in .env}"
: "${TELEGRAM_API_HASH:?TELEGRAM_API_HASH is not set in .env}"

BIN="$APP_DIR/vendor/telegram-bot-api/bin/telegram-bot-api"
if [[ ! -x "$BIN" ]]; then
  echo "telegram-bot-api is not built. Run scripts/build-bot-api.sh" >&2
  exit 1
fi

mkdir -p "$APP_DIR/data/bot-api" "$APP_DIR/data/bot-api-tmp" "$HOME/Library/Logs/telegram-stt"

# --local makes getFile return an absolute path on this disk instead of a
# download URL, and lifts the 20 MB file cap to 2000 MB.
exec "$BIN" \
  --local \
  --http-port="${BOT_API_PORT:-8081}" \
  --http-ip-address=127.0.0.1 \
  --dir="$APP_DIR/data/bot-api" \
  --temp-dir="$APP_DIR/data/bot-api-tmp" \
  --verbosity="${BOT_API_VERBOSITY:-1}" \
  --log="$HOME/Library/Logs/telegram-stt/bot-api.log"
