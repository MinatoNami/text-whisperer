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

echo "==> waiting for the model to warm up"
for _ in $(seq 1 40); do
  grep -q "model ready" "$LOG_DIR/worker.out.log" 2>/dev/null && break
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
if curl -sS -o /dev/null --max-time 5 "http://127.0.0.1:8081/" 2>/dev/null; then
  echo "    bot api: responding"
else
  echo "    bot api: not responding"; ok=0
fi
# The real question: can a system daemon reach the GPU with nobody logged in?
if tail -40 "$LOG_DIR/worker.out.log" 2>/dev/null | grep -q "model ready"; then
  echo "    GPU:     Metal reachable from the daemon (model warmed up)"
else
  echo "    GPU:     model has NOT warmed up — check $LOG_DIR/worker.err.log"
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
