"""Endpoints server-to-server : appelés par les BACKENDS des apps consommatrices
(Débloque-moi côté Next.js server, LectureAI côté FastAPI, etc.) pour vérifier
un user, débiter ou créditer son portefeuille.

Authentification : header ``X-Api-Key: <secret>`` + JWT user dans ``Authorization``
ou ``X-User-Id`` direct (l'app a déjà vérifié son user).

La clé API est définie via env var ``S2S_API_KEYS`` au format :
   ``debloquemoi:secret123,lecturai:abc456``
Chaque entrée associe un ``app_id`` à un secret partagé.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.orm import Session

from credits_wallet import (
    as_utc_aware,
    credit_credits,
    debit_credits,
    utc_now,
    wallet_block_reason,
)
from database import get_db
from models import User, WalletTransaction
from pricing import wallet_units_to_mru_display

router = APIRouter(tags=["s2s"])


def _load_s2s_keys() -> dict[str, str]:
    """Parse env ``S2S_API_KEYS`` au format ``app_id:secret,app_id2:secret2``."""
    raw = os.getenv("S2S_API_KEYS", "").strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        app_id, secret = pair.split(":", 1)
        app_id = app_id.strip()
        secret = secret.strip()
        if app_id and secret and len(secret) >= 16:
            out[secret] = app_id
    return out


def require_s2s_app(
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
) -> str:
    """Retourne l'``app_id`` associé à la clé API présentée. 401 sinon."""
    keys = _load_s2s_keys()
    if not keys:
        raise HTTPException(
            status_code=500,
            detail="Aucune clé S2S configurée côté serveur (env S2S_API_KEYS).",
        )
    if not x_api_key:
        raise HTTPException(status_code=401, detail="X-Api-Key manquant.")
    app_id = keys.get(x_api_key.strip())
    if not app_id:
        raise HTTPException(status_code=403, detail="X-Api-Key invalide.")
    return app_id


def _user_or_404(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    return u


# ─── /s2s/wallet/me ──────────────────────────────────────────────────────────


class WalletMeResponse(BaseModel):
    user_id: int
    balance_units: int
    balance_mru: float
    blocked_reason: Optional[str] = None
    expires_at: Optional[str] = None


@router.get("/wallet/me", response_model=WalletMeResponse)
def wallet_me(
    user_id: int,
    db: Session = Depends(get_db),
    app_id: str = Depends(require_s2s_app),  # noqa: ARG001
):
    u = _user_or_404(db, user_id)
    return WalletMeResponse(
        user_id=u.id,
        balance_units=int(u.credit_balance),
        balance_mru=wallet_units_to_mru_display(int(u.credit_balance)),
        blocked_reason=wallet_block_reason(u),
        expires_at=u.credits_expire_at.isoformat() if u.credits_expire_at else None,
    )


# ─── /s2s/wallet/debit ───────────────────────────────────────────────────────


class DebitBody(BaseModel):
    user_id: int
    amount_units: int = Field(gt=0, description="Quantité d'unités à débiter (entier > 0).")
    external_ref: Optional[str] = Field(
        default=None, max_length=128, description="ID externe (réponse API, etc.)."
    )
    note: Optional[str] = Field(default=None, max_length=256)


class DebitResponse(BaseModel):
    user_id: int
    debited_units: int
    balance_units: int
    balance_mru: float
    transaction_id: int


@router.post("/wallet/debit", response_model=DebitResponse)
def wallet_debit(
    body: DebitBody,
    db: Session = Depends(get_db),
    app_id: str = Depends(require_s2s_app),
):
    """Débite le portefeuille **après** un succès côté l'app consommatrice.

    Pas de pré-check de solde ici : l'app doit appeler ``/wallet/me`` AVANT
    pour décider si elle peut servir la requête. Le débit autorise un solde
    négatif léger (anti API-drain) — voir credits_wallet.debit_credits.
    """
    u = _user_or_404(db, body.user_id)
    new_balance, debited = debit_credits(db, u, body.amount_units)
    if new_balance is None:
        raise HTTPException(status_code=500, detail="Échec débit.")
    tx = WalletTransaction(
        user_id=u.id,
        app_id=app_id,
        direction="debit",
        amount_units=debited,
        balance_after=new_balance,
        external_ref=body.external_ref,
        note=body.note,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return DebitResponse(
        user_id=u.id,
        debited_units=debited,
        balance_units=new_balance,
        balance_mru=wallet_units_to_mru_display(new_balance),
        transaction_id=tx.id,
    )


# ─── /s2s/wallet/credit ──────────────────────────────────────────────────────


class CreditBody(BaseModel):
    user_id: int
    amount_units: int = Field(gt=0)
    external_ref: Optional[str] = Field(default=None, max_length=128)
    note: Optional[str] = Field(default=None, max_length=256)


# ─── /s2s/free-hint/consume ──────────────────────────────────────────────────


class FreeHintConsumeBody(BaseModel):
    user_id: int


class FreeHintConsumeResponse(BaseModel):
    consumed: bool
    remaining: int
    expires_at: Optional[str] = None
    reason: Optional[str] = None  # "expired" | "exhausted" | None si consumed=True


@router.post("/free-hint/consume", response_model=FreeHintConsumeResponse)
def free_hint_consume(
    body: FreeHintConsumeBody,
    db: Session = Depends(get_db),
    app_id: str = Depends(require_s2s_app),  # noqa: ARG001
):
    """Consomme atomiquement un upload gratuit pour ce user, si disponible.

    Retourne ``consumed=False`` si la deadline est dépassée ou si le solde de
    free hints est nul. Dans ce cas, l'app appelante doit basculer sur le
    flux MRU classique (``/s2s/wallet/me`` + ``/s2s/wallet/debit``).
    """
    u = _user_or_404(db, body.user_id)
    fh_exp = as_utc_aware(u.free_hints_expires_at)
    now = utc_now()
    if fh_exp is None or fh_exp < now:
        # On nettoie le solde résiduel : la deadline a primé.
        if (u.free_hints_remaining or 0) > 0:
            u.free_hints_remaining = 0
            db.add(u)
            db.commit()
        return FreeHintConsumeResponse(
            consumed=False,
            remaining=0,
            expires_at=fh_exp.isoformat() if fh_exp else None,
            reason="expired",
        )
    # UPDATE atomique : on décrémente seulement si le solde est > 0. SQLite
    # sérialise les écritures, donc deux requêtes parallèles ne pourront pas
    # toutes deux consommer le dernier free hint (la seconde verra rowcount=0).
    result = db.execute(
        update(User)
        .where(User.id == u.id, User.free_hints_remaining > 0)
        .values(free_hints_remaining=User.free_hints_remaining - 1)
    )
    db.commit()
    if result.rowcount == 0:
        return FreeHintConsumeResponse(
            consumed=False,
            remaining=0,
            expires_at=fh_exp.isoformat(),
            reason="exhausted",
        )
    db.refresh(u)
    return FreeHintConsumeResponse(
        consumed=True,
        remaining=int(u.free_hints_remaining),
        expires_at=fh_exp.isoformat(),
        reason=None,
    )


@router.post("/wallet/credit", response_model=DebitResponse)
def wallet_credit(
    body: CreditBody,
    db: Session = Depends(get_db),
    app_id: str = Depends(require_s2s_app),
):
    """Crédite le portefeuille (remboursement après échec API, bonus exceptionnel...).

    Pour les recharges officielles, passer plutôt par ``/api/credits/topup``
    qui passe par la validation admin.
    """
    u = _user_or_404(db, body.user_id)
    new_balance, credited = credit_credits(db, u, body.amount_units)
    if new_balance is None:
        raise HTTPException(status_code=500, detail="Échec crédit.")
    tx = WalletTransaction(
        user_id=u.id,
        app_id=app_id,
        direction="credit",
        amount_units=credited,
        balance_after=new_balance,
        external_ref=body.external_ref,
        note=body.note,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return DebitResponse(
        user_id=u.id,
        debited_units=credited,
        balance_units=new_balance,
        balance_mru=wallet_units_to_mru_display(new_balance),
        transaction_id=tx.id,
    )
