#!/usr/bin/env bash
# Snapshot SQLite + .env de auth-api. Idempotent, safe-to-rerun.
#
# - Utilise `sqlite3 .backup` (online-safe : marche pendant que le serveur
#   écrit, contrairement à un simple cp qui peut corrompre un WAL en cours).
# - Conserve les 30 derniers backups (auto-rotation, plus que ça consommerait
#   du disque pour rien).
# - Output un seul timestamp en fin si tout OK, code de sortie non-zéro si KO.
#
# Usage :
#   bash /opt/auth-api/scripts/backup.sh
#   bash /opt/auth-api/scripts/backup.sh /tmp/somewhere-else.db   # dest custom

set -euo pipefail

DB="${AUTH_API_DB:-/opt/auth-api/data/auth-api.db}"
BACKUP_DIR="${AUTH_API_BACKUP_DIR:-/opt/auth-api/data/backups}"
ENV_FILE="${AUTH_API_ENV:-/opt/auth-api/.env}"
KEEP="${AUTH_API_BACKUPS_KEEP:-30}"

if [ ! -f "$DB" ]; then
  echo "[backup] ERREUR : DB introuvable à $DB" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
TS=$(date +%Y%m%d-%H%M%S)
DB_DEST="${1:-$BACKUP_DIR/auth-api-$TS.db}"
ENV_DEST="$BACKUP_DIR/env-$TS.txt"

# Backup atomique via SQLite (locks proprement la DB)
sqlite3 "$DB" ".backup '$DB_DEST'"

# Vérifie intégrité du backup
if ! sqlite3 "$DB_DEST" "PRAGMA integrity_check;" | grep -q '^ok$'; then
  echo "[backup] ERREUR : intégrité du backup KO ($DB_DEST)" >&2
  rm -f -- "$DB_DEST"
  exit 2
fi

# Snapshot du .env aussi (contient les secrets S2S, JWT, etc.)
if [ -f "$ENV_FILE" ]; then
  cp -p "$ENV_FILE" "$ENV_DEST"
  chmod 600 "$ENV_DEST"
fi

# Rotation : garde les N plus récents (.db + .txt en parallèle)
ls -1t "$BACKUP_DIR"/auth-api-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --
ls -1t "$BACKUP_DIR"/env-*.txt 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --

SIZE=$(du -h "$DB_DEST" | cut -f1)
COUNT=$(ls -1 "$BACKUP_DIR"/auth-api-*.db 2>/dev/null | wc -l | tr -d ' ')
echo "[backup] OK $DB_DEST ($SIZE) — $COUNT backups conservés"
