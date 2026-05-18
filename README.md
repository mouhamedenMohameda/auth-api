# auth-api

Backend partagé d'**authentification + portefeuille MRU + top-ups** pour les apps RIM (Débloque-moi, LectureAI, futurs projets).

Extrait du backend LectureAI, dépouillé de tout le code transcription/WhatsApp/Telegram. **Un seul compte utilisateur, un seul portefeuille, plusieurs apps consommatrices.**

## Démarrage local

```bash
cd auth-api
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Édite .env : JWT_SECRET (32+ chars aléatoires), S2S_API_KEYS, ADMIN_EMAIL
.venv/bin/uvicorn main:app --reload --port 8000
```

Docs interactives : http://localhost:8000/docs

## Endpoints

### 🔐 Auth utilisateur (`/api/auth/...`)

| Endpoint | Description |
|---|---|
| `POST /api/auth/register` | Inscription (email, password, nni, whatsapp, referral_code optionnel) |
| `POST /api/auth/login` | Connexion → JWT |
| `GET  /api/auth/me` | Profil de l'utilisateur courant |
| `POST /api/auth/reset-password` | Réinit (email + nni + whatsapp → new_password) |

### 💸 Crédits & top-ups (`/api/credits/...`)

| Endpoint | Description |
|---|---|
| `GET  /api/credits/me` | Solde + expiration + blocage éventuel |
| `POST /api/credits/topup-requests` | Demande de recharge (upload preuve virement) |
| `GET  /api/credits/topup-requests/mine` | Mes demandes |

### 👮 Admin (`/api/admin/...`)

| Endpoint | Description |
|---|---|
| `GET  /api/admin/credit-topups?status=pending` | Lister demandes |
| `GET  /api/admin/credit-topups/{id}/proof` | Télécharger preuve |
| `POST /api/admin/credit-topups/{id}/approve` | Approuver |
| `POST /api/admin/credit-topups/{id}/reject` | Rejeter |
| `GET  /api/admin/users/search?email=...` | Chercher utilisateur |
| `POST /api/admin/users/{id}/grant-wallet` | Créditer manuellement |

### 🤝 Parrainage (`/api/referrals/me`)

Code public + stats du compte courant.

### 🔌 Server-to-server (`/s2s/wallet/...`) — pour les apps consommatrices

**Authentification** : header `X-Api-Key: <secret>` (clé dédiée par app, définie dans `S2S_API_KEYS`).

| Endpoint | Description |
|---|---|
| `GET  /s2s/wallet/me?user_id=X` | Lire solde + statut d'un user |
| `POST /s2s/wallet/debit` | Débiter après succès API |
| `POST /s2s/wallet/credit` | Créditer (remboursement, bonus...) |

## Workflow typique d'une app consommatrice (ex: Débloque-moi)

```
Élève → Débloque-moi (Next.js)
              │
              │ 1. Login → POST /api/auth/login → JWT
              │ 2. Frontend stocke JWT dans cookie httpOnly
              │
              │ 3. L'élève clique "Solution complète"
              ▼
       Débloque-moi backend (API route Next.js)
              │
              │ 4. Vérifie JWT, extrait user_id
              │ 5. GET /s2s/wallet/me?user_id=X → vérifie solde >= prix
              │ 6. Appelle Groq → réponse OK
              │ 7. POST /s2s/wallet/debit { user_id, amount_units, app_id="debloquemoi" }
              │ 8. Retourne réponse à l'élève
              ▼
        auth-api (FastAPI) sur api.rim.dev
              │
              │ Persiste tout dans la DB partagée
              ▼
          Postgres / SQLite
```

## Migration depuis LectureAI

Si tu as déjà une DB LectureAI avec des users :

```bash
# 1. Backup
cp /chemin/lecturai.db ./data/auth-api.db

# 2. Lance auth-api : les colonnes existantes (email, password_hash, nni, etc.)
#    sont conservées. Les colonnes transcription/whatsapp/telegram restent en
#    base mais sont ignorées par les nouveaux modèles.
.venv/bin/uvicorn main:app --port 8000

# 3. LectureAI doit pointer sur auth-api pour login/credits au lieu de
#    son backend interne. Garde son propre backend uniquement pour la
#    transcription.
```

## Sécurité

- **JWT** : HS256, secret 32+ chars, expire en 14 jours.
- **Bcrypt** pour les mots de passe (passlib).
- **Rate-limiting** sur `/auth/register` et `/auth/login` (slowapi).
- **Clés S2S** : 16+ chars, stockées en env, jamais loggées.
- **Row-locking** sur tous les débits/crédits pour éviter les races.

## Variables d'environnement clés

```ini
AUTH_REQUIRED=true
JWT_SECRET=...          # 32+ chars aléatoires
DATABASE_URL=...        # défaut: sqlite:///./data/auth-api.db
CORS_ALLOWED_ORIGINS=http://localhost:3000,https://bac.rim.dev
S2S_API_KEYS=debloquemoi:secret1,lecturai:secret2
ADMIN_EMAIL=mohameda.mouhameden@gmail.com
MRU_PER_USD=40
MARGIN_MULTIPLIER=2
```

## Déploiement Contabo

```bash
# Sur le serveur
git clone <repo>/auth-api && cd auth-api
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env  # éditer

# Service systemd
sudo tee /etc/systemd/system/auth-api.service <<'EOF'
[Unit]
Description=auth-api FastAPI
After=network.target

[Service]
User=mohameda
WorkingDirectory=/opt/auth-api
ExecStart=/opt/auth-api/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now auth-api

# Caddy
cat >> /etc/caddy/Caddyfile <<'EOF'
api.rim.dev {
    reverse_proxy localhost:8000
}
EOF
systemctl reload caddy
```
