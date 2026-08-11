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
# Off-box copy: this script only writes locally. Point BACKUP_DIR at a
# mounted remote volume, or add an rsync/rclone line after the loop
# below once a specific off-box destination is chosen — deliberately
# not hard-coded to one vendor here.
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
