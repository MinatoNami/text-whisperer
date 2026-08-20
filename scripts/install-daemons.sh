#!/usr/bin/env bash
# Install the services as system daemons, so they start at boot with nobody
# logged in.
#
#     sudo ./scripts/install-daemons.sh          install and start
#     sudo ./scripts/install-daemons.sh --remove uninstall, back to agents
#
# Why this exists: launchd *agents* live in a login session. After a reboot
# that leaves the Mac at the login window there is no session, the agents never
# start, and the bot is silently dead. Daemons have no such dependency.
#
# The GPU still works: MLX reaches Metal without a GUI session. This script
# verifies that after installing rather than assuming it.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }

# Who owns the checkout — the daemons run as them, not as root.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OWNER="$(stat -f "%Su" "$APP_DIR")"
OWNER_HOME="$(dscl . -read "/Users/$OWNER" NFSHomeDirectory | awk '{print $2}')"
LOG_DIR="$OWNER_HOME/Library/Logs/telegram-stt"
DAEMON_DIR="/Library/LaunchDaemons"
AGENT_DIR="$OWNER_HOME/Library/LaunchAgents"
LABELS=(com.telegram-stt.bot-api com.telegram-stt.worker)

stop_agents() {
  # Agents and daemons with the same label would fight over the port and the
  # bot token, so the agents go first.
  for label in "${LABELS[@]}"; do
    sudo -u "$OWNER" launchctl bootout "gui/$(id -u "$OWNER")/${label}"  2>/dev/null || true
    sudo -u "$OWNER" launchctl bootout "user/$(id -u "$OWNER")/${label}" 2>/dev/null || true
    rm -f "$AGENT_DIR/${label}.plist"
  done
  pkill -f "telegram_stt|vendor/telegram-bot-api/bin" 2>/dev/null || true
}

if [[ "${1:-}" == "--remove" ]]; then
  for label in "${LABELS[@]}"; do
    launchctl bootout "system/${label}" 2>/dev/null || true
    rm -f "$DAEMON_DIR/${label}.plist"
  done
  echo "daemons removed. Re-install the agents with: ./scripts/ctl.sh install"
  exit 0
fi

echo "==> app:   $APP_DIR"
echo "==> user:  $OWNER ($OWNER_HOME)"

# The worker log is append-only, so a "model ready" from an earlier run would
# pass the check below without the daemon having done anything. Remember where
# the log ends now and only look past that point.
WORKER_LOG="$OWNER_HOME/Library/Logs/telegram-stt/worker.out.log"
WATERMARK=$(wc -l < "$WORKER_LOG" 2>/dev/null | tr -d " " || echo 0)
WATERMARK=${WATERMARK:-0}

stop_agents
mkdir -p "$LOG_DIR"
chown "$OWNER" "$LOG_DIR"

for label in "${LABELS[@]}"; do
  src="$APP_DIR/launchd/${label}.daemon.plist.tmpl"
  [[ -f "$src" ]] || { echo "missing template: $src" >&2; exit 1; }
  dst="$DAEMON_DIR/${label}.plist"
  sed -e "s|__APP_DIR__|$APP_DIR|g" \
      -e "s|__LOG_DIR__|$LOG_DIR|g" \
      -e "s|__USER__|$OWNER|g" \
      -e "s|__HOME__|$OWNER_HOME|g" "$src" > "$dst"
  # launchd refuses a daemon plist that is group- or world-writable.
  chown root:wheel "$dst"
  chmod 644 "$dst"
  launchctl bootout "system/${label}" 2>/dev/null || true
  launchctl bootstrap system "$dst"
  echo "==> installed $label"
done

# Only lines written after the watermark count as this run's.
new_worker_log() { tail -n +$((WATERMARK + 1)) "$WORKER_LOG" 2>/dev/null; }

echo "==> waiting for the services to come up"
# The Bot API server logs into Telegram over MTProto before it will answer, so
# both of these need polling rather than one impatient check.
api_ok=0 gpu_ok=0
for _ in $(seq 1 45); do
  [[ $api_ok -eq 0 ]] && curl -sS -o /dev/null --max-time 3 "http://127.0.0.1:8081/" 2>/dev/null && api_ok=1
  [[ $gpu_ok -eq 0 ]] && new_worker_log | grep -q "model ready" && gpu_ok=1
  [[ $api_ok -eq 1 && $gpu_ok -eq 1 ]] && break
  sleep 2
done

echo
echo "==> verifying"
ok=1
if launchctl print "system/com.telegram-stt.worker" >/dev/null 2>&1; then
  echo "    worker:  loaded in the system domain"
else
  echo "    worker:  NOT loaded"; ok=0
fi
if [[ $api_ok -eq 1 ]]; then
  echo "    bot api: responding on 127.0.0.1:8081"
else
  echo "    bot api: not responding — see $LOG_DIR/bot-api.err.log"; ok=0
fi
# The real question: can a system daemon reach the GPU with nobody logged in?
if [[ $gpu_ok -eq 1 ]]; then
  echo "    GPU:     Metal reachable from the daemon (model warmed up just now)"
else
  echo "    GPU:     model did NOT warm up — check $LOG_DIR/worker.err.log"
  echo "             if Metal is unavailable to daemons on this macOS, use"
  echo "             automatic login with the agents instead:"
  echo "               sudo ./scripts/install-daemons.sh --remove"
  ok=0
fi

echo
if [[ $ok -eq 1 ]]; then
  echo "==> done. These now start at boot with nobody logged in."
else
  echo "==> installed, but something is not healthy. See above." >&2
  exit 1
fi
