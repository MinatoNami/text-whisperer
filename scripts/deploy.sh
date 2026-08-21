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
#   ./scripts/deploy.sh --backup      pull the remote archive to this Mac
#   ./scripts/deploy.sh --ui          tunnel the web app here and open it
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

# The short hostname resolves via whatever is providing DNS at the time, which
# is not always there; the mDNS .local name usually is. Try both rather than
# failing when only one works.
resolve_host() {
  local candidates=("$REMOTE_HOST")
  [[ "$REMOTE_HOST" != *.* ]] && candidates+=("${REMOTE_HOST}.local")
  for candidate in "${candidates[@]}"; do
    if ssh -o ConnectTimeout=8 -o BatchMode=yes "$candidate" true 2>/dev/null; then
      if [[ "$candidate" != "$REMOTE_HOST" ]]; then
        say "reached it as ${candidate} (${REMOTE_HOST} did not resolve)"
        REMOTE_HOST="$candidate"
      fi
      return 0
    fi
  done
  return 1
}
ctl()    { remote "cd ~/$REMOTE_DIR && ./scripts/ctl.sh $*"; }

require_ssh() {
  if resolve_host; then
    return 0
  fi
  warn "cannot reach $REMOTE_HOST (tried ${REMOTE_HOST} and ${REMOTE_HOST}.local)."
  warn "if it is asleep, wake it; if key auth is the problem: ssh-copy-id $REMOTE_HOST"
  exit 1
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
    --exclude '.pytest_cache/' \
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
  --backup)
    require_ssh
    DEST="${2:-${BACKUP_DEST:-$HOME/Backups/telegram-stt}}"
    mkdir -p "$DEST"
    say "pulling ~/$REMOTE_DIR/data/archive from $REMOTE_HOST"
    # No --delete: the backup is append-only, so a mishap on the M4 Pro can
    # never erase transcripts already copied here.
    rsync -a --human-readable --partial --itemize-changes \
      "$REMOTE_HOST:$REMOTE_DIR/data/archive/" "$DEST/" | tail -15
    say "backup holds $(du -sh "$DEST" 2>/dev/null | cut -f1), \
$(find "$DEST" -name '*.txt' 2>/dev/null | wc -l | tr -d ' ') transcript(s)"
    ;;
  --ui)
    require_ssh
    PORT="${WEB_PORT:-8090}"
    # The UI binds to loopback on the target on purpose, so reaching it means
    # forwarding a port rather than exposing one.
    if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      say "something already listens on $PORT here; not starting a second tunnel"
    else
      ssh -f -N -L "${PORT}:127.0.0.1:${PORT}" "$REMOTE_HOST"
      say "tunnel open: localhost:${PORT} -> ${REMOTE_HOST}:${PORT}"
    fi
    if curl -sS -o /dev/null --max-time 5 "http://127.0.0.1:${PORT}/"; then
      say "http://127.0.0.1:${PORT}"
      command -v open >/dev/null && open "http://127.0.0.1:${PORT}" || true
    else
      warn "tunnel is up but nothing answered — is the worker running? (--status)"
    fi
    echo "${DIM}   close it with: pkill -f '${PORT}:127.0.0.1:${PORT}'${RESET}"
    ;;
  --funnel|--funnel-off)
    require_ssh
    PORT="${WEB_PORT:-8090}"
    if [ "$1" = "--funnel-off" ]; then
      remote "tailscale funnel --bg off || tailscale funnel off"
      say "funnel closed — the app is back to tailnet-only"
      exit 0
    fi
    # Funnel publishes to the entire internet. Without a password that means
    # every transcript is readable by anyone who finds the hostname, so check
    # the deployed .env rather than trusting that it was set.
    if ! ssh "$REMOTE_HOST" "grep -qE '^WEB_PASSWORD=.+' ~/$REMOTE_DIR/.env" 2>/dev/null; then
      warn "refusing: WEB_PASSWORD is empty in the deployed .env"
      echo "${DIM}   Funnel is public. Set a password first:${RESET}"
      echo "${DIM}     python3 -c 'import secrets; print(secrets.token_urlsafe(18))'${RESET}"
      echo "${DIM}   put it in .env as WEB_PASSWORD, set WEB_PUBLIC=1, then redeploy.${RESET}"
      exit 1
    fi
    if ! ssh "$REMOTE_HOST" "grep -qE '^WEB_PUBLIC=1' ~/$REMOTE_DIR/.env" 2>/dev/null; then
      warn "WEB_PUBLIC is not 1 — the session cookie will not be marked Secure"
      echo "${DIM}   set WEB_PUBLIC=1 in .env and redeploy before opening this up.${RESET}"
      exit 1
    fi
    remote "tailscale funnel --bg $PORT"
    say "public URL:"
    remote "tailscale funnel status" 2>/dev/null | sed 's/^/   /'
    echo "${DIM}   send the URL and the password separately.${RESET}"
    echo "${DIM}   close it again with: ./scripts/deploy.sh --funnel-off${RESET}"
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
