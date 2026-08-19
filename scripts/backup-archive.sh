#!/usr/bin/env bash
# Copy the transcript archive somewhere it will survive this disk dying.
#
#   ./scripts/backup-archive.sh                 # uses BACKUP_DEST from .env
#   ./scripts/backup-archive.sh ~/Backups/stt   # or an explicit destination
#   ./scripts/backup-archive.sh user@host:path  # rsync remote works too
#
# Append-only by design: --delete is NOT passed, so a mistake at the source can
# never wipe the backup. Prune it by hand if it ever needs it.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

set -a
# shellcheck disable=SC1091
[[ -f .env ]] && source .env
set +a

SRC="${ARCHIVE_DIR:-$APP_DIR/data/archive}"
DEST="${1:-${BACKUP_DEST:-}}"

if [[ -z "$DEST" ]]; then
  cat >&2 <<'MSG'
No destination. Either pass one:
    ./scripts/backup-archive.sh ~/Backups/telegram-stt
or set BACKUP_DEST in .env, for example:
    BACKUP_DEST=~/Library/Mobile Documents/com~apple~CloudDocs/telegram-stt
    BACKUP_DEST=/Volumes/Backup/telegram-stt
    BACKUP_DEST=someuser@nas.local:/volume1/backups/telegram-stt
MSG
  exit 64
fi

if [[ ! -d "$SRC" ]]; then
  echo "nothing to back up: $SRC does not exist" >&2
  exit 1
fi

# Local destinations need creating; remote ones rsync handles itself.
if [[ "$DEST" != *:* ]]; then
  mkdir -p "$DEST"
fi

echo "==> $SRC"
echo "==> $DEST"
rsync -a --human-readable --partial \
      --exclude '.DS_Store' \
      --itemize-changes \
      "$SRC/" "$DEST/" | tail -20

if [[ "$DEST" != *:* ]]; then
  echo "==> backup now holds $(du -sh "$DEST" 2>/dev/null | cut -f1), \
$(find "$DEST" -name '*.txt' 2>/dev/null | wc -l | tr -d ' ') transcript(s)"
fi
echo "==> done"
