#!/usr/bin/env bash
#
# Nightly backup of the three SQLite databases — the only state that
# can't be rebuilt from source (journal.db: every detection ever made;
# users.db: accounts, watchlists, outbox; universe.db: the active-symbol
# cache, cheaply rebuildable but backed up anyway since it's small).
#
# Uses sqlite3's `.backup` command, not `cp`, so a backup taken while a
# process is mid-write never captures a torn file — `.backup` takes
# SQLite's own read lock and copies page-by-page, safe to run against a
# live database with no downtime.
#
# Usage:
#   scripts/backup.sh                  # backup to ./backups/
#   BACKUP_DIR=/mnt/offbox scripts/backup.sh
#   RETAIN_DAYS=30 scripts/backup.sh   # default is 14
#
# Off-box copy: journal.db, users.db, and .env are additionally shipped
# off-box, GPG-symmetric-encrypted (AES256) before upload — these
# backups sit in a third party's object storage, so they're never
# uploaded in plaintext, .env especially since it's real credentials.
# Opt-in via RCLONE_REMOTE + BACKUP_ENCRYPTION_PASSPHRASE_FILE; unset
# means "off-box shipping not configured yet" — skip with a clear
# message, not a hard failure, same as this project's other
# optional-integration fallbacks (e.g. DevEmailSender when
# RESEND_API_KEY is unset). See docs/DEPLOYMENT.md for one-time setup
# (recommends DigitalOcean Spaces — same provider as the VPS, no new
# vendor relationship, S3-compatible so rclone needs no DO-specific
# code) and the tested restore procedure.
#
# universe.db is NOT shipped off-box — cheaply rebuildable from Alpaca's
# asset catalog (tradebot.universe.refresh_universe), unlike the other
# two, which are the only copies of real subscriber/detection history.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$REPO_ROOT/data"
BACKUP_DIR="${BACKUP_DIR:-$REPO_ROOT/backups}"
RETAIN_DAYS="${RETAIN_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$BACKUP_DIR"

for db in journal users universe; do
  src="$DATA_DIR/$db.db"
  [[ -f "$src" ]] || continue
  dest="$BACKUP_DIR/${db}_${STAMP}.db"
  sqlite3 "$src" ".backup '$dest'"
  gzip "$dest"
  echo "backed up $db.db -> $(basename "$dest").gz"
done

find "$BACKUP_DIR" -name "*.db.gz" -mtime "+$RETAIN_DAYS" -delete

echo "done. retained $(find "$BACKUP_DIR" -name '*.db.gz' | wc -l | tr -d ' ') backup file(s)."

# --- Off-box shipping (optional, opt-in) ---------------------------------
if [[ -z "${RCLONE_REMOTE:-}" ]]; then
  echo "RCLONE_REMOTE not set — off-box shipping skipped (backups remain local-only; see docs/DEPLOYMENT.md)"
elif [[ -z "${BACKUP_ENCRYPTION_PASSPHRASE_FILE:-}" || ! -f "$BACKUP_ENCRYPTION_PASSPHRASE_FILE" ]]; then
  echo "RCLONE_REMOTE is set but BACKUP_ENCRYPTION_PASSPHRASE_FILE is missing or unreadable — refusing to ship backups unencrypted. Skipping off-box step." >&2
else
  OFFBOX_STAGE="$(mktemp -d)"
  trap 'rm -rf "$OFFBOX_STAGE"' EXIT

  encrypt_and_stage() {
    local src="$1" name="$2"
    gpg --batch --yes --pinentry-mode loopback \
        --passphrase-file "$BACKUP_ENCRYPTION_PASSPHRASE_FILE" \
        --symmetric --cipher-algo AES256 \
        -o "$OFFBOX_STAGE/${name}.gpg" "$src"
  }

  # journal.db / users.db: encrypt the gzip'd backups just made above.
  # universe.db is deliberately excluded — see the module docstring.
  for db in journal users; do
    gz="$BACKUP_DIR/${db}_${STAMP}.db.gz"
    [[ -f "$gz" ]] && encrypt_and_stage "$gz" "${db}_${STAMP}.db.gz"
  done
  # .env: not a sqlite db, no .backup step needed — straight encrypt.
  [[ -f "$REPO_ROOT/.env" ]] && encrypt_and_stage "$REPO_ROOT/.env" "env_${STAMP}"

  RCLONE_CONFIG="${RCLONE_CONFIG:-$HOME/.config/rclone/rclone.conf}"
  rclone copy "$OFFBOX_STAGE" "$RCLONE_REMOTE" --config "$RCLONE_CONFIG"
  echo "shipped off-box to $RCLONE_REMOTE: $(ls "$OFFBOX_STAGE" | tr '\n' ' ')"

  # Off-box retention — same RETAIN_DAYS window as the local copies,
  # keyed off the remote's own modification time since rclone has no
  # -mtime-style flag on `copy`/`delete` the way `find` does locally.
  rclone delete "$RCLONE_REMOTE" --min-age "${RETAIN_DAYS}d" --config "$RCLONE_CONFIG" 2>/dev/null || true
fi
