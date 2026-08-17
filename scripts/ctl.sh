#!/usr/bin/env bash
# Service control for the two launchd agents. Runs ON the target machine;
# deploy.sh drives it over ssh.
set -euo pipefail

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$HOME/Library/Logs/telegram-stt"
AGENT_DIR="$HOME/Library/LaunchAgents"
LABELS=(com.telegram-stt.bot-api com.telegram-stt.worker)

# Metal needs the Aqua session, so gui/<uid> is the correct domain. Fall back
# to user/<uid> when nobody is logged in at the console (headless ssh).
DOMAIN="gui/$(id -u)"
launchctl print "$DOMAIN" >/dev/null 2>&1 || DOMAIN="user/$(id -u)"

render_plists() {
  mkdir -p "$AGENT_DIR" "$LOG_DIR"
  for label in "${LABELS[@]}"; do
    local src="$APP_DIR/launchd/${label}.plist.tmpl"
    local dst="$AGENT_DIR/${label}.plist"
    [[ -f "$src" ]] || { echo "missing template: $src" >&2; exit 1; }
    sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__LOG_DIR__|$LOG_DIR|g" "$src" >"$dst"
  done
}

cmd_install() {
  render_plists
  cmd_stop >/dev/null 2>&1 || true
  for label in "${LABELS[@]}"; do
    launchctl bootstrap "$DOMAIN" "$AGENT_DIR/${label}.plist"
    launchctl enable "$DOMAIN/${label}"
  done
  echo "installed into $DOMAIN"
}

cmd_start() {
  for label in "${LABELS[@]}"; do
    launchctl kickstart "$DOMAIN/${label}" >/dev/null
  done
  echo "started"
}

cmd_stop() {
  for label in "${LABELS[@]}"; do
    launchctl bootout "$DOMAIN/${label}" 2>/dev/null || true
  done
  echo "stopped"
}

cmd_restart() {
  render_plists
  for label in "${LABELS[@]}"; do
    if launchctl print "$DOMAIN/${label}" >/dev/null 2>&1; then
      launchctl kickstart -k "$DOMAIN/${label}" >/dev/null
    else
      launchctl bootstrap "$DOMAIN" "$AGENT_DIR/${label}.plist"
    fi
  done
  echo "restarted"
}

cmd_status() {
  echo "domain:  $DOMAIN"
  echo "app dir: $APP_DIR"
  for label in "${LABELS[@]}"; do
    if info=$(launchctl print "$DOMAIN/${label}" 2>/dev/null); then
      pid=$(awk -F'= ' '/^\tpid = /{print $2; exit}' <<<"$info")
      code=$(awk -F'= ' '/last exit code = /{print $2; exit}' <<<"$info")
      printf '%-28s pid=%-8s last_exit=%s\n' "$label" "${pid:-—}" "${code:-—}"
    else
      printf '%-28s not loaded\n' "$label"
    fi
  done
  echo
  local url="${BOT_API_BASE_URL:-http://127.0.0.1:8081}"
  # A 404 on / is the healthy answer — it means the server is listening.
  if code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "$url" 2>/dev/null); then
    echo "bot api:  responding (http $code) at $url"
  else
    echo "bot api:  UNREACHABLE at $url"
  fi
}

cmd_logs() {
  mkdir -p "$LOG_DIR"
  shopt -s nullglob
  local files=("$LOG_DIR"/*.log)
  if (( ${#files[@]} == 0 )); then
    echo "no logs yet in $LOG_DIR — has the service started?"
    exit 0
  fi
  tail -n "${LINES:-80}" -F "${files[@]}"
}

cmd_uninstall() {
  cmd_stop
  for label in "${LABELS[@]}"; do
    rm -f "$AGENT_DIR/${label}.plist"
  done
  echo "uninstalled"
}

cmd_logout_cloud() {
  # A bot can only live on one Bot API server at a time. Telegram requires an
  # explicit logOut against the cloud API before a local server will accept it.
  set -a
  # shellcheck disable=SC1091
  [[ -f "$APP_DIR/.env" ]] && source "$APP_DIR/.env"
  set +a
  : "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN is not set in .env}"
  echo "logging the bot out of api.telegram.org…"
  curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/logOut"
  echo
  echo "Wait ~10 minutes before the local server will accept it, then: ctl.sh restart"
}

case "${1:-status}" in
  install)      cmd_install ;;
  start)        cmd_start ;;
  stop)         cmd_stop ;;
  restart)      cmd_restart ;;
  status)       cmd_status ;;
  logs)         cmd_logs ;;
  uninstall)    cmd_uninstall ;;
  logout-cloud) cmd_logout_cloud ;;
  *)
    echo "usage: ctl.sh {install|start|stop|restart|status|logs|uninstall|logout-cloud}" >&2
    exit 64
    ;;
esac
