#!/usr/bin/env bash
# Restaure la DB depuis un backup. À utiliser SEULEMENT si tu as cassé qch
# (mauvaise migration, données effacées, etc.).
#
# Usage :
#   bash /opt/auth-api/scripts/restore.sh                  # liste les backups
#   bash /opt/auth-api/scripts/restore.sh latest           # restaure le plus récent
#   bash /opt/auth-api/scripts/restore.sh 20260519-143215  # par timestamp

set -euo pipefail

DB="${AUTH_API_DB:-/opt/auth-api/data/auth-api.db}"
BACKUP_DIR="${AUTH_API_BACKUP_DIR:-/opt/auth-api/data/backups}"
SERVICE="${AUTH_API_SERVICE:-auth-api}"

list_backups() {
  echo "Backups disponibles dans $BACKUP_DIR :"
  ls -1tlh "$BACKUP_DIR"/auth-api-*.db 2>/dev/null | head -30 | awk '{print $NF, $5}' || true
}

if [ $# -eq 0 ]; then
  list_backups
  echo
  echo "Usage : $0 latest | <TIMESTAMP>"
  exit 0
fi

if [ "$1" = "latest" ]; then
  SRC=$(ls -1t "$BACKUP_DIR"/auth-api-*.db 2>/dev/null | head -1 || true)
  if [ -z "$SRC" ]; then echo "Aucun backup trouvé." >&2; exit 1; fi
else
  SRC="$BACKUP_DIR/auth-api-$1.db"
  if [ ! -f "$SRC" ]; then echo "Backup introuvable : $SRC" >&2; list_backups; exit 1; fi
fi

echo "Tu vas restaurer la DB depuis :"
echo "  $SRC ($(du -h "$SRC" | cut -f1))"
echo "vers : $DB"
echo
read -p "Confirmer ? (tape 'oui') " ANSWER
if [ "$ANSWER" != "oui" ]; then echo "Annulé."; exit 0; fi

# Backup défensif de l'état actuel avant restore
echo "→ Backup défensif de l'état courant"
bash "$(dirname "$0")/backup.sh"

echo "→ Stop $SERVICE"
systemctl stop "$SERVICE"

echo "→ Copie du backup vers $DB"
cp -p "$SRC" "$DB"

# Vérifie intégrité après copie
if ! sqlite3 "$DB" "PRAGMA integrity_check;" | grep -q '^ok$'; then
  echo "ERREUR : intégrité de la DB restaurée KO" >&2
  exit 2
fi

echo "→ Start $SERVICE"
systemctl start "$SERVICE"
sleep 2
systemctl status "$SERVICE" --no-pager | head -5

echo "✅ Restore OK"
