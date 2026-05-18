from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from credits_wallet import wallet_block_reason
from database import get_db
from deps import auth_required, get_current_user_optional, require_user
from models import CreditTopUpRequest, User
from pricing import (
    MARGIN_MULTIPLIER,
    MRU_PER_USD,
    MRU_WALLET_MICRO,
    wallet_units_to_mru_display,
)

TOPUP_ROOT = Path(__file__).resolve().parent.parent / "data" / "topups"
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_IMG_BYTES = 6 * 1024 * 1024

router = APIRouter(tags=["credits"])


@router.get("/credits/me")
def credits_me(
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    if not auth_required():
        return {
            "feature_enabled": False,
            "message": "Le portefeuille MRU ne s’applique pas lorsque l’authentification est désactivée.",
        }
    if user is None:
        raise HTTPException(status_code=401, detail="Connecte-toi pour voir ton solde.")
    u = db.get(User, user.id)
    if not u:
        raise HTTPException(status_code=401, detail="Session invalide.")
    blocked = wallet_block_reason(u)
    bal_units = int(u.credit_balance)
    return {
        "feature_enabled": True,
        "credit_balance": bal_units,
        "balance_mru": wallet_units_to_mru_display(bal_units),
        "credits_expire_at": u.credits_expire_at.isoformat() if u.credits_expire_at else None,
        "can_use_features": blocked is None,
        "block_reason": blocked,
        "email": u.email,
        "is_admin": bool(u.is_admin),
    }


@router.get("/credits/pricing-info")
def credits_pricing_info():
    """Paramètres publics pour afficher previews (sans clés ni tarifs détaillés)."""
    return {
        "mru_per_usd": MRU_PER_USD,
        "customer_margin_multiplier": MARGIN_MULTIPLIER,
        "wallet_micro_per_mru": MRU_WALLET_MICRO,
    }


@router.post("/credits/topup-requests")
async def create_topup_request(
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(require_user),
    file: UploadFile = File(...),
):
    if not auth_required():
        raise HTTPException(
            status_code=403, detail="La recharge est indisponible sans authentification."
        )
    if user is None:
        raise HTTPException(status_code=401, detail="Connexion requise.")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail="Format non accepté : envoie une capture PNG, JPG, WEBP ou GIF.",
        )
    raw = await file.read()
    if len(raw) > MAX_IMG_BYTES:
        raise HTTPException(status_code=400, detail="Fichier trop volumineux (max 6 Mo).")
    TOPUP_ROOT.mkdir(parents=True, exist_ok=True)
    user_dir = TOPUP_ROOT / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}{ext}"
    rel = f"{user.id}/{stored}"
    (user_dir / stored).write_bytes(raw)
    row = CreditTopUpRequest(
        user_id=user.id,
        stored_filename=rel,
        original_filename=(file.filename or "capture")[:240],
        status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "status": row.status,
        "message": "Demande enregistrée. Un administrateur créditera ton compte après vérification du virement.",
    }


@router.get("/credits/topup-requests/mine")
def my_topup_requests(
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(require_user),
):
    if user is None:
        raise HTTPException(status_code=401, detail="Connexion requise.")
    rows = (
        db.execute(
            select(CreditTopUpRequest)
            .where(CreditTopUpRequest.user_id == user.id)
            .order_by(CreditTopUpRequest.id.desc())
            .limit(50)
        )
        .scalars()
        .all()
    )
    return {
        "requests": [
            {
                "id": r.id,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
                "credits_granted": r.credits_granted,
                "granted_mru_approx": (
                    wallet_units_to_mru_display(r.credits_granted)
                    if r.credits_granted is not None
                    else None
                ),
                "admin_note": r.admin_note,
            }
            for r in rows
        ],
    }
