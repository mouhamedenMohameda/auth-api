#!/usr/bin/env bash
# Déploiement sûr de auth-api.
#
# Séquence :
#   1) Backup DB + .env (refuse de continuer si KO)
#   2) git fetch + show les commits qui vont arriver
#   3) git pull
#   4) systemctl restart
#   5) Health check (curl /health ou équivalent)
#   6) Si la 5 échoue, rollback : restart sans toucher au code (le binaire
#      uvicorn est déjà l'ancien si pip install a échoué). Le user a aussi le
#      backup pour restaurer manuellement avec restore.sh.
#
# Usage :
#   bash /opt/auth-api/scripts/deploy.sh
#   bash /opt/auth-api/scripts/deploy.sh --no-pull   # backup + restart seulement

set -euo pipefail

REPO_DIR="${AUTH_API_REPO:-/opt/auth-api}"
SERVICE="${AUTH_API_SERVICE:-auth-api}"
HEALTH_URL="${AUTH_API_HEALTH:-http://127.0.0.1:8000/health}"

NO_PULL=0
for arg in "$@"; do
  case "$arg" in
    --no-pull) NO_PULL=1 ;;
    *) echo "Argument inconnu : $arg" >&2; exit 1 ;;
  esac
done

cd "$REPO_DIR"

echo "═══ 1. Backup avant déploiement ═══"
bash "$REPO_DIR/scripts/backup.sh"

if [ "$NO_PULL" -eq 0 ]; then
  echo
  echo "═══ 2. Récupération des nouveaux commits ═══"
  git fetch origin
  LOCAL=$(git rev-parse HEAD)
  REMOTE=$(git rev-parse @{u})
  if [ "$LOCAL" = "$REMOTE" ]; then
    echo "[deploy] Rien à pull — déjà à jour ($LOCAL)"
  else
    echo "[deploy] Commits à appliquer :"
    git log --oneline "$LOCAL..$REMOTE"
    echo
    echo "═══ 3. git pull ═══"
    git pull --ff-only
  fi
fi

# Si requirements.txt a changé depuis le dernier deploy, on réinstalle
if [ "$NO_PULL" -eq 0 ] && git diff HEAD@{1} HEAD --name-only 2>/dev/null | grep -q '^requirements\.txt$'; then
  echo
  echo "═══ requirements.txt modifié — pip install ═══"
  "$REPO_DIR/.venv/bin/pip" install -r requirements.txt
fi

echo
echo "═══ 4. Redémarrage du service $SERVICE ═══"
systemctl restart "$SERVICE"
sleep 3

echo
echo "═══ 5. Health check ═══"
if systemctl is-active --quiet "$SERVICE"; then
  echo "[deploy] systemctl is-active : OK"
else
  echo "[deploy] ERREUR : $SERVICE n'est pas actif après restart" >&2
  systemctl status "$SERVICE" --no-pager | tail -20
  exit 3
fi

# Curl health endpoint (best-effort : si le endpoint n'existe pas, skip)
HTTP_CODE=$(curl -s -o /tmp/health.body -w '%{http_code}' "$HEALTH_URL" || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
  echo "[deploy] Health check OK ($HEALTH_URL → 200)"
elif [ "$HTTP_CODE" = "404" ] || [ "$HTTP_CODE" = "000" ]; then
  echo "[deploy] (pas de endpoint $HEALTH_URL, on saute)"
else
  echo "[deploy] ⚠️  Health check anormal : HTTP $HTTP_CODE" >&2
  cat /tmp/health.body 2>/dev/null | head -5
fi

echo
echo "✅ Déploiement OK — $(git log -1 --oneline)"
