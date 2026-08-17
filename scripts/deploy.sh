#!/usr/bin/env bash
# Deploy telegram-stt to the MacBook Pro 14" M4 Pro.
#
#   ./scripts/deploy.sh --bootstrap   first run: brew deps + build the Bot API
#                                     server + install launchd agents
#   ./scripts/deploy.sh               sync code, uv sync, restart services
#   ./scripts/deploy.sh --status      service + health check
#   ./scripts/deploy.sh --logs        tail remote logs
#   ./scripts/deploy.sh --stop        unload both services
#   ./scripts/deploy.sh --shell       ssh into the app dir
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-macbook-pro-14-m4-pro}"
REMOTE_DIR="${REMOTE_DIR:-apps/telegram-stt}"   # relative to the remote $HOME
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; RESET=$'\033[0m'
say()  { echo "${BOLD}==>${RESET} $*"; }
warn() { echo "${RED}!!${RESET} $*" >&2; }

# A non-interactive ssh shell does not source the profile, so Homebrew is not
# on PATH. Every remote command gets it prepended.
REMOTE_PATH='export PATH=/opt/homebrew/bin:/opt/homebrew/sbin:$PATH;'
remote() { ssh -o ConnectTimeout=10 "$REMOTE_HOST" "$REMOTE_PATH $*"; }
ctl()    { remote "cd ~/$REMOTE_DIR && ./scripts/ctl.sh $*"; }

require_ssh() {
  if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "$REMOTE_HOST" true 2>/dev/null; then
    warn "cannot ssh to $REMOTE_HOST with key auth."
    warn "fix it with:  ssh-copy-id $REMOTE_HOST"
    exit 1
  fi
}

require_env() {
  if [[ ! -f "$LOCAL_DIR/.env" ]]; then
    warn "no .env found. Copy .env.example to .env and fill it in first."
    exit 1
  fi
  local missing=()
  for key in TELEGRAM_API_ID TELEGRAM_API_HASH TELEGRAM_BOT_TOKEN; do
    grep -qE "^${key}=.+" "$LOCAL_DIR/.env" || missing+=("$key")
  done
  if (( ${#missing[@]} )); then
    warn "these are empty in .env: ${missing[*]}"
    exit 1
  fi
}

sync_code() {
  say "syncing to $REMOTE_HOST:~/$REMOTE_DIR"
  remote "mkdir -p ~/$REMOTE_DIR"
  rsync -az --delete \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude 'vendor/' \
    --exclude 'data/' \
    --exclude 'logs/' \
    --exclude '__pycache__/' \
    --exclude '.DS_Store' \
    "$LOCAL_DIR/" "$REMOTE_HOST:$REMOTE_DIR/"
  remote "chmod +x ~/$REMOTE_DIR/scripts/*.sh; chmod 600 ~/$REMOTE_DIR/.env 2>/dev/null || true"
}

bootstrap() {
  say "installing Homebrew dependencies"
  remote 'command -v brew >/dev/null || { echo "Homebrew missing — install from https://brew.sh" >&2; exit 1; }
          brew install uv ffmpeg || true'

  say "building telegram-bot-api from source (15-30 min on a cold build)"
  remote "cd ~/$REMOTE_DIR && ./scripts/build-bot-api.sh"

  say "installing launchd agents"
  ctl install
}

sync_deps() {
  say "resolving Python environment"
  remote "cd ~/$REMOTE_DIR && HF_HOME=~/$REMOTE_DIR/data/huggingface uv sync"
}

case "${1:-}" in
  --status)
    require_ssh; ctl status
    ;;
  --logs)
    require_ssh
    say "tailing ~/Library/Logs/telegram-stt (ctrl-c to stop)"
    ssh -t "$REMOTE_HOST" "cd ~/$REMOTE_DIR && ./scripts/ctl.sh logs"
    ;;
  --stop)
    require_ssh; ctl stop
    ;;
  --restart)
    require_ssh; ctl restart
    ;;
  --shell)
    require_ssh
    ssh -t "$REMOTE_HOST" "cd ~/$REMOTE_DIR && exec \$SHELL -l"
    ;;
  --uninstall)
    require_ssh; ctl uninstall
    ;;
  --logout-cloud)
    require_ssh; ctl logout-cloud
    ;;
  --bootstrap)
    require_ssh; require_env
    sync_code
    bootstrap
    sync_deps
    ctl restart
    echo
    say "bootstrap complete"
    echo "${DIM}   first transcription downloads the Whisper weights (~1.6 GB)${RESET}"
    ctl status
    ;;
  "")
    require_ssh; require_env
    sync_code
    sync_deps
    ctl restart
    ctl status
    ;;
  *)
    sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 64
    ;;
esac
