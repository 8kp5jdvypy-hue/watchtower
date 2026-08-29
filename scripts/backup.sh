#!/usr/bin/env bash
#
# Nightly backup of every durable SQLite database plus immutable postmarket
# audits/control evidence. journal.db, users.db, evaluations.db, and
# postmarket_shadow.db are required: silently omitting one would create a
# backup set that cannot reconstruct Perch's decisions or evidence chain.
# universe.db remains optional because it is rebuildable from the asset catalog.
#
# Uses Python sqlite3's online backup API, not `cp`, so a snapshot taken while a
# process is mid-write never captures a torn file. Every snapshot is checked
# with PRAGMA quick_check before compression. A SHA-256 manifest binds the
# complete set and is consumed by scripts/verify_backup.py during restore drills.
#
# Usage:
#   scripts/backup.sh                  # backup to ./backups/
#   BACKUP_DIR=/mnt/offbox scripts/backup.sh
#   RETAIN_DAYS=30 scripts/backup.sh   # default is 14
#
# Off-box copy: every irrebuildable database, the postmarket artifact archive,
# the set manifest, and .env are additionally shipped off-box,
# GPG-symmetric-encrypted (AES256) before upload — these
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
# databases and postmarket artifacts.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="${DATA_DIR:-$REPO_ROOT/data}"
BACKUP_DIR="${BACKUP_DIR:-$REPO_ROOT/backups}"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
RETAIN_DAYS="${RETAIN_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REQUIRED_DBS=(journal users evaluations postmarket_shadow)
OPTIONAL_DBS=(universe)
OFFBOX_DBS=(journal users evaluations postmarket_shadow)
GENERATED=()

if [[ ! "$RETAIN_DAYS" =~ ^[0-9]+$ ]]; then
  echo "RETAIN_DAYS must be a non-negative integer" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

for db in "${REQUIRED_DBS[@]}"; do
  src="$DATA_DIR/$db.db"
  if [[ ! -f "$src" ]]; then
    echo "required database missing: $src" >&2
    exit 1
  fi
done

backup_database() {
  local db="$1" src="$DATA_DIR/$1.db" dest="$BACKUP_DIR/${1}_${STAMP}.db"
  [[ -f "$src" ]] || return 0
  python3 "$SCRIPT_DIR/sqlite_snapshot.py" "$src" "$dest"
  gzip "$dest"
  gzip -t "${dest}.gz"
  GENERATED+=("$(basename "${dest}.gz")")
  echo "backed up $db.db -> $(basename "$dest").gz"
}

for db in "${REQUIRED_DBS[@]}" "${OPTIONAL_DBS[@]}"; do
  backup_database "$db"
done

ARTIFACT_INPUTS=()
for directory in postmarket_audits postmarket_evidence; do
  [[ -d "$DATA_DIR/$directory" ]] && ARTIFACT_INPUTS+=("$directory")
done
for contract in \
  postmarket_customer_delivery_policy.json \
  postmarket_customer_delivery_authorization.json \
  postmarket_customer_dry_run_campaign.json; do
  [[ -f "$DATA_DIR/$contract" ]] && ARTIFACT_INPUTS+=("$contract")
done
if (( ${#ARTIFACT_INPUTS[@]} > 0 )); then
  artifact_name="postmarket_artifacts_${STAMP}.tar.gz"
  COPYFILE_DISABLE=1 tar -C "$DATA_DIR" -czf \
    "$BACKUP_DIR/$artifact_name" "${ARTIFACT_INPUTS[@]}"
  python3 "$SCRIPT_DIR/verify_backup.py" \
    --check-artifact-archive "$BACKUP_DIR/$artifact_name"
  GENERATED+=("$artifact_name")
  echo "backed up postmarket artifacts -> $artifact_name"
else
  echo "no postmarket audit/evidence/contracts exist yet — artifact archive skipped"
fi

manifest_name="manifest_${STAMP}.sha256"
(
  cd "$BACKUP_DIR"
  sha256sum "${GENERATED[@]}" > "$manifest_name"
)
echo "wrote backup manifest -> $manifest_name"

find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name "journal_*.db.gz" -o -name "users_*.db.gz" \
     -o -name "evaluations_*.db.gz" -o -name "postmarket_shadow_*.db.gz" \
     -o -name "universe_*.db.gz" -o -name "postmarket_artifacts_*.tar.gz" \
     -o -name "manifest_*.sha256" \) \
  -mtime "+$RETAIN_DAYS" -delete

echo "done. manifest=$manifest_name files=${#GENERATED[@]}"

# --- Off-box shipping (optional, opt-in) ---------------------------------
if [[ -z "${RCLONE_REMOTE:-}" ]]; then
  echo "RCLONE_REMOTE not set — off-box shipping skipped (backups remain local-only; see docs/DEPLOYMENT.md)"
elif [[ "$RCLONE_REMOTE" != *:* || -z "${RCLONE_REMOTE#*:}" ]]; then
  echo "RCLONE_REMOTE must include a non-empty path after the remote name; refusing remote-root access." >&2
  exit 1
elif [[ -z "${BACKUP_ENCRYPTION_PASSPHRASE_FILE:-}" || ! -f "$BACKUP_ENCRYPTION_PASSPHRASE_FILE" ]]; then
  echo "RCLONE_REMOTE is set but BACKUP_ENCRYPTION_PASSPHRASE_FILE is missing or unreadable — refusing to downgrade to local-only backup." >&2
  exit 1
else
  OFFBOX_STAGE="$(mktemp -d)"
  trap 'rm -rf "$OFFBOX_STAGE"' EXIT
  OFFBOX_MANIFEST_FILES=()

  encrypt_and_stage() {
    local src="$1" name="$2"
    gpg --batch --yes --pinentry-mode loopback \
        --passphrase-file "$BACKUP_ENCRYPTION_PASSPHRASE_FILE" \
        --symmetric --cipher-algo AES256 \
        -o "$OFFBOX_STAGE/${name}.gpg" "$src"
  }

  # Every irrebuildable database is encrypted. universe.db is deliberately
  # excluded because it can be rebuilt from the asset catalog.
  for db in "${OFFBOX_DBS[@]}"; do
    gz="$BACKUP_DIR/${db}_${STAMP}.db.gz"
    if [[ -f "$gz" ]]; then
      name="${db}_${STAMP}.db.gz"
      encrypt_and_stage "$gz" "$name"
      OFFBOX_MANIFEST_FILES+=("$name")
    fi
  done
  artifact="$BACKUP_DIR/postmarket_artifacts_${STAMP}.tar.gz"
  if [[ -f "$artifact" ]]; then
    artifact_name="$(basename "$artifact")"
    encrypt_and_stage "$artifact" "$artifact_name"
    OFFBOX_MANIFEST_FILES+=("$artifact_name")
  fi

  # The local manifest includes optional universe.db when present, but that
  # rebuildable database is intentionally not shipped off-box. Build the
  # encrypted remote manifest from the exact off-box payload instead of
  # uploading a manifest that references a file recovery cannot download.
  offbox_manifest_plain="$OFFBOX_STAGE/${manifest_name}.plain"
  (
    cd "$BACKUP_DIR"
    sha256sum "${OFFBOX_MANIFEST_FILES[@]}" > "$offbox_manifest_plain"
  )
  encrypt_and_stage "$offbox_manifest_plain" "$manifest_name"
  rm -f "$offbox_manifest_plain"
  # .env: not a sqlite db, no .backup step needed — straight encrypt.
  [[ -f "$ENV_FILE" ]] && encrypt_and_stage "$ENV_FILE" "env_${STAMP}"

  # Systemd services get no $HOME unless the unit sets one, and this
  # line used to crash under set -u the moment that was true. Falling
  # back to /root keeps this script correct under any caller (systemd,
  # cron, a manual run) without requiring every caller to remember to
  # set HOME.
  RCLONE_CONFIG="${RCLONE_CONFIG:-${HOME:-/root}/.config/rclone/rclone.conf}"
  rclone copy "$OFFBOX_STAGE" "$RCLONE_REMOTE" --config "$RCLONE_CONFIG"
  echo "shipped off-box to $RCLONE_REMOTE: $(ls "$OFFBOX_STAGE" | tr '\n' ' ')"

  # Off-box retention — same RETAIN_DAYS window as the local copies,
  # keyed off the remote's own modification time since rclone has no
  # -mtime-style flag on `copy`/`delete` the way `find` does locally.
  rclone delete "$RCLONE_REMOTE" --min-age "${RETAIN_DAYS}d" --config "$RCLONE_CONFIG" 2>/dev/null || true
fi
