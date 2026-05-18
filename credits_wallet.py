"""Débits et crédits sur ``User.credit_balance`` (entier d’unités ; sémantique et arrondi MRU → unités : ``models.User``, ``pricing``)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import User


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def wallet_block_reason(user: User, now: Optional[datetime] = None) -> Optional[str]:
    """Retourne un message français si le portefeuille bloque l'usage."""
    now = now or utc_now()
    if user.credit_balance <= 0:
        return "Solde MRU insuffisant. Effectue un virement puis envoie ta preuve depuis l’écran « Crédits » (validation par l’admin)."
    exp = user.credits_expire_at
    if exp is not None:
        exp_aware = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
        if exp_aware <= now:
            return "La validité de ton portefeuille a expiré. Envoie une demande de recharge avec ta preuve de virement pour qu’un administrateur rallonge ta validité."
    return None


def assert_wallet_can_use(db: Session, user: Optional[User]) -> Optional[User]:
    """À l’entrée d’une route : vérifie solde et date d’expiration. Retourne l’utilisateur à jour depuis la DB."""
    if user is None:
        return None
    u = db.get(User, user.id)
    if not u:
        raise HTTPException(status_code=401, detail="Session invalide.")
    reason = wallet_block_reason(u)
    if reason:
        raise HTTPException(status_code=403, detail=reason)
    return u


def debit_credits(db: Session, user: Optional[User], amount: int) -> Tuple[Optional[int], int]:
    """
    Débite après succès. Retourne (nouveau solde ou None si pas de user), montant réel débité.
    Sécurisé avec un verrou de ligne (with_for_update) et autorise le découvert pour stopper l'API Drain.
    """
    if user is None or amount <= 0:
        return None, 0
    u = db.query(User).filter(User.id == user.id).with_for_update().first()
    if not u:
        return None, 0
        
    u.credit_balance -= amount
    db.add(u)
    db.commit()
    db.refresh(u)
    return u.credit_balance, amount


def credit_credits(db: Session, user: Optional[User], amount: int) -> Tuple[Optional[int], int]:
    """
    Crédite le portefeuille (remboursement / libération de réserve). Pas de contrôle d’expiration.
    Retourne (nouveau solde ou None), montant réellement ajouté.
    Sécurisé avec un verrou de ligne (with_for_update).
    """
    if user is None or amount <= 0:
        return None, 0
    u = db.query(User).filter(User.id == user.id).with_for_update().first()
    if not u:
        return None, 0
    u.credit_balance += int(amount)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u.credit_balance, int(amount)
