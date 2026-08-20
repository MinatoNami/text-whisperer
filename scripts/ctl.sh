#!/usr/bin/env bash
# Service control for the two launchd agents. Runs ON the target machine;
# deploy.sh drives it over ssh.
set -euo pipefail

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$HOME/Library/Logs/telegram-stt"
AGENT_DIR="$HOME/Library/LaunchAgents"
LABELS=(com.telegram-stt.bot-api com.telegram-stt.worker)

# If the services were installed as system daemons, they own the labels and
# this script must not create agents alongside them — two copies fight over the
# port and the bot token. Detect that first.
DAEMON_DIR="/Library/LaunchDaemons"
MODE="agent"
for label in com.telegram-stt.bot-api com.telegram-stt.worker; do
  [[ -f "$DAEMON_DIR/${label}.plist" ]] && MODE="daemon"
done

# launchd *agents* live in a login session. gui/<uid> is the right domain when
# someone is logged in at the console; user/<uid> is the fallback.
DOMAIN="gui/$(id -u)"
launchctl print "$DOMAIN" >/dev/null 2>&1 || DOMAIN="user/$(id -u)"
[[ "$MODE" == "daemon" ]] && DOMAIN="system"

# `Bootstrap failed: 5: Input/output error` says nothing useful. The usual
# cause is that nobody is logged in, so there is no session to host an agent.
explain_domain_failure() {
  local console
  console="$(stat -f "%Su" /dev/console 2>/dev/null || echo unknown)"
  echo >&2
  echo "!! launchd would not start the services in $DOMAIN." >&2
  if [[ "$console" == "root" || -z "$(who 2>/dev/null)" ]]; then
    cat >&2 <<'MSG'
   Nobody is logged in at the console on this Mac, so there is no login
   session for a launchd *agent* to live in. This happens after a reboot
   when the machine is left at the login window.

   Fix it one of these ways:
     - log in at the console, then re-run this command; or
     - enable automatic login (System Settings > Users & Groups), so a
       reboot always lands in a session; or
     - install the services as system daemons, which need no login:
           sudo ./scripts/install-daemons.sh
MSG
  else
    echo "   Someone is logged in ($console), so this is not the usual" >&2
    echo "   'no session' case. Try: launchctl print $DOMAIN" >&2
  fi
  echo >&2
}

render_plists() {
  mkdir -p "$AGENT_DIR" "$LOG_DIR"
  for label in "${LABELS[@]}"; do
    local src="$APP_DIR/launchd/${label}.plist.tmpl"
    local dst="$AGENT_DIR/${label}.plist"
    [[ -f "$src" ]] || { echo "missing template: $src" >&2; exit 1; }
    sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__LOG_DIR__|$LOG_DIR|g" "$src" >"$dst"
  done
}

# bootout is asynchronous and the worker can take its whole poll window to
# exit; bootstrapping while the old instance is still going gives EIO.
wait_until_gone() {
  local label="$1" deadline=$((SECONDS + 40))
  while launchctl print "$DOMAIN/${label}" >/dev/null 2>&1; do
    (( SECONDS > deadline )) && return 1
    sleep 1
  done
  return 0
}

cmd_install() {
  if [[ "$MODE" == "daemon" ]]; then
    echo "system daemons are installed; not creating agents alongside them." >&2
    echo "use: sudo ./scripts/install-daemons.sh   (or --remove to go back)" >&2
    exit 1
  fi
  render_plists
  cmd_stop >/dev/null 2>&1 || true
  for label in "${LABELS[@]}"; do
    wait_until_gone "$label" || echo "warning: $label is still shutting down" >&2
    if ! launchctl bootstrap "$DOMAIN" "$AGENT_DIR/${label}.plist" 2>&1; then
      explain_domain_failure
      exit 1
    fi
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
  if [[ "$MODE" == "daemon" ]]; then
    echo "these are system daemons; stopping them needs root:" >&2
    echo "  sudo launchctl bootout system/com.telegram-stt.worker" >&2
    echo "  sudo launchctl bootout system/com.telegram-stt.bot-api" >&2
    exit 1
  fi
  for label in "${LABELS[@]}"; do
    launchctl bootout "$DOMAIN/${label}" 2>/dev/null || true
  done
  echo "stopped"
}

cmd_restart() {
  if [[ "$MODE" == "daemon" ]]; then
    # Restarting a system daemon needs root, but KeepAlive does the job for
    # free: kill the processes and launchd brings them straight back on the
    # new code. That keeps deploys working without sudo.
    pkill -f "python -m telegram_stt" 2>/dev/null || true
    pkill -f "vendor/telegram-bot-api/bin" 2>/dev/null || true
    for _ in $(seq 1 40); do
      pgrep -f "python -m telegram_stt" >/dev/null 2>&1 && break
      sleep 1
    done
    echo "restarted (system daemons, via KeepAlive)"
    return
  fi
  render_plists
  for label in "${LABELS[@]}"; do
    if launchctl print "$DOMAIN/${label}" >/dev/null 2>&1; then
      launchctl kickstart -k "$DOMAIN/${label}" >/dev/null
    elif ! launchctl bootstrap "$DOMAIN" "$AGENT_DIR/${label}.plist" 2>&1; then
      explain_domain_failure
      exit 1
    fi
  done
  echo "restarted"
}

cmd_status() {
  echo "mode:    $MODE"
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
