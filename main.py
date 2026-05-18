"""auth-api — backend partagé d'authentification, portefeuille MRU et top-ups.

Sert plusieurs apps front-end (Débloque-moi, LectureAI, futurs projets) avec :
  - Inscription / connexion / reset (JWT, bcrypt).
  - Portefeuille en unités MRU avec expiration de validité.
  - Top-ups par preuve de virement validés par l'admin.
  - Parrainage avec bonus en cascade.
  - Endpoints S2S (clé API) pour débit/crédit côté serveur des apps consommatrices.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from admin_sync import sync_designated_admin
from database import Base, engine
from deps import auth_required
from env_validation import validate_env
from models import (  # noqa: F401 — charge les tables
    CreditTopUpRequest,
    ReferralEvent,
    User,
    WalletTransaction,
)
from rate_limit import install_rate_limiter
from routes import admin_credits, auth, credits, referrals as referrals_routes, s2s
from schema_migrate import (
    ensure_credit_schema,
    ensure_free_hints_schema,
    ensure_referrals_schema,
)
from security import jwt_secret

_env_file = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_file, override=True)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _data = Path(__file__).resolve().parent / "data"
    _data.mkdir(parents=True, exist_ok=True)
    validate_env(auth_required=auth_required())
    if auth_required():
        jwt_secret()
    Base.metadata.create_all(bind=engine)
    ensure_credit_schema(engine)
    ensure_referrals_schema(engine)
    ensure_free_hints_schema(engine)
    try:
        sync_designated_admin()
    except Exception as e:
        logger.warning("admin sync failed: %s", e)
    yield


app = FastAPI(
    title="auth-api",
    description="Auth + portefeuille MRU + top-ups partagés entre apps RIM.",
    version="0.1.0",
    lifespan=lifespan,
)

install_rate_limiter(app)

# CORS : autoriser les origines des apps front-end. Configurable via env var
# CORS_ALLOWED_ORIGINS (séparé par virgule). Par défaut : localhost dev.
_origins_env = os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000")
_origins = [o.strip() for o in _origins_env.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
app.include_router(credits.router, prefix="/api")
app.include_router(admin_credits.router, prefix="/api")
app.include_router(referrals_routes.router, prefix="/api")
app.include_router(s2s.router, prefix="/s2s")


@app.get("/health")
def health():
    return {"status": "ok", "auth_required": auth_required()}
