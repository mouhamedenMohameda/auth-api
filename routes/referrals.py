"""Endpoints de parrainage (lecture seule côté user)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from deps import require_user
from models import User
from pricing import wallet_units_to_mru_display
from referrals import referral_stats

router = APIRouter(tags=["referrals"])


@router.get("/referrals/me")
def my_referral_info(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    """Code de parrainage + URL de partage + stats agrégées du connecté."""
    fresh = db.get(User, user.id)
    stats = referral_stats(db, fresh)
    # Affichage MRU pour l'UI (les unités brutes ne parlent pas à l'utilisateur).
    return {
        **stats,
        "bonus_signup_mru": wallet_units_to_mru_display(int(stats.get("bonus_signup_units") or 0)),
        "bonus_paid_mru_referrer": wallet_units_to_mru_display(int(stats.get("bonus_paid_units_referrer") or 0)),
        "bonus_paid_mru_referred": wallet_units_to_mru_display(int(stats.get("bonus_paid_units_referred") or 0)),
    }
