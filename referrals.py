"""Système de parrainage : code unique par user, bonus double-face avec garde-fous anti-fraude.

Deux étapes :
  - ``signup``  : petit bonus immédiat aux deux côtés (effet psychologique du parrain qui invite).
  - ``first_paid_topup_approved`` : gros bonus quand le filleul fait sa 1ère recharge approuvée.
    Cette étape est la VRAIE valeur (le filleul a prouvé qu'il est un user réel, pas un bot).

Tous les bonus sont en **unités portefeuille** (cf. ``pricing.MRU_WALLET_MICRO``). Les montants par défaut
sont conservateurs et surchargeables via env. Idempotence garantie par ``UniqueConstraint(referred_user_id, kind)``.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from credits_wallet import credit_credits
from models import ReferralEvent, User
from pricing import MRU_WALLET_MICRO

logger = logging.getLogger(__name__)

# Alphabet sans caractères ambigus (0/O/I/1) — lisibilité en partage WhatsApp / oral.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        v = int(raw)
        return max(0, v)
    except ValueError:
        return default


def signup_bonus_units() -> int:
    """Bonus immédiat des deux côtés (parrain + filleul) au signup. Défaut : 1 MRU (peu, juste pour l'effet)."""
    raw = os.getenv("REFERRAL_SIGNUP_BONUS_UNITS")
    if raw is None or str(raw).strip() == "":
        return 1 * MRU_WALLET_MICRO
    return _env_int("REFERRAL_SIGNUP_BONUS_UNITS", 1 * MRU_WALLET_MICRO)


def paid_topup_bonus_units_referrer() -> int:
    """Gros bonus au parrain quand le filleul fait sa 1ère recharge. Défaut : 10 MRU."""
    raw = os.getenv("REFERRAL_PAID_TOPUP_BONUS_UNITS_REFERRER")
    if raw is None or str(raw).strip() == "":
        return 10 * MRU_WALLET_MICRO
    return _env_int("REFERRAL_PAID_TOPUP_BONUS_UNITS_REFERRER", 10 * MRU_WALLET_MICRO)


def paid_topup_bonus_units_referred() -> int:
    """Bonus côté filleul à sa 1ère recharge approuvée. Défaut : 5 MRU."""
    raw = os.getenv("REFERRAL_PAID_TOPUP_BONUS_UNITS_REFERRED")
    if raw is None or str(raw).strip() == "":
        return 5 * MRU_WALLET_MICRO
    return _env_int("REFERRAL_PAID_TOPUP_BONUS_UNITS_REFERRED", 5 * MRU_WALLET_MICRO)


def referrer_max_active_referrals() -> int:
    """Plafond filleuls par parrain pour limiter l'abus (devient « ambassadeur » manuel au-delà)."""
    return _env_int("REFERRAL_MAX_PER_REFERRER", 50)


def generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def generate_unique_code(db: Session, max_attempts: int = 12) -> str:
    """Génère un code unique en testant l'unicité en base (collision improbable mais on garde un retry)."""
    for _ in range(max_attempts):
        c = generate_code()
        exists = db.execute(select(User.id).where(User.referral_code == c)).first()
        if exists is None:
            return c
    # Cas pathologique (alphabet épuisé) : on rallonge.
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH + 4))


def normalize_code(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    # Caractères non-alphabet : on rejette plutôt que de réparer (un code partagé erroné = échec silencieux préférable).
    if not all(ch in _CODE_ALPHABET for ch in s):
        return None
    if len(s) < 6 or len(s) > 16:
        return None
    return s


def backfill_missing_referral_codes(db: Session) -> int:
    """Attribue un code à chaque user qui n'en a pas (one-shot, idempotent). Retourne le nombre de codes posés."""
    users = db.execute(select(User).where(User.referral_code.is_(None))).scalars().all()
    n = 0
    for u in users:
        u.referral_code = generate_unique_code(db)
        db.add(u)
        n += 1
        if n % 50 == 0:
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
    if n:
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.exception("Collision improbable lors du backfill referral_code")
    return n


def find_referrer_by_code(db: Session, code: Optional[str]) -> Optional[User]:
    norm = normalize_code(code)
    if norm is None:
        return None
    return db.execute(select(User).where(User.referral_code == norm)).scalar_one_or_none()


def _count_active_referrals(db: Session, referrer_id: int) -> int:
    n = db.execute(
        select(func.count(User.id)).where(User.referred_by_user_id == referrer_id)
    ).scalar()
    return int(n or 0)


def attach_referrer_and_apply_signup_bonus(
    db: Session,
    new_user: User,
    code: Optional[str],
) -> Optional[User]:
    """À appeler dans `/auth/register` après commit du new_user. Idempotent.

    Garde-fous :
      - code invalide → ignore silencieusement
      - self-parrainage (email/NNI/WhatsApp identique au parrain) → refusé
      - parrain au plafond → refusé
      - new_user déjà parrainé → refusé
    """
    if new_user is None or new_user.id is None:
        return None
    if new_user.referred_by_user_id is not None:
        return None

    referrer = find_referrer_by_code(db, code)
    if referrer is None or referrer.id == new_user.id:
        return None

    # Anti self-referral basique : mêmes identifiants directs.
    if (
        (referrer.email or "").strip().lower() == (new_user.email or "").strip().lower()
        or (referrer.nni or "").strip() == (new_user.nni or "").strip()
        or (referrer.whatsapp_phone or "").strip() == (new_user.whatsapp_phone or "").strip()
    ):
        return None

    if _count_active_referrals(db, referrer.id) >= referrer_max_active_referrals():
        logger.info("Parrain %s a atteint le plafond actif — pas de bonus.", referrer.id)
        return None

    new_user.referred_by_user_id = referrer.id
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    bonus = signup_bonus_units()
    if bonus <= 0:
        return referrer

    # Crédit deux côtés + trace idempotente.
    event = ReferralEvent(
        referrer_user_id=referrer.id,
        referred_user_id=new_user.id,
        kind="signup",
        referrer_bonus_units=bonus,
        referred_bonus_units=bonus,
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        # Déjà appliqué (idempotence) — on ne crédite pas deux fois.
        db.rollback()
        return referrer

    credit_credits(db, referrer, bonus)
    credit_credits(db, new_user, bonus)
    return referrer


def apply_first_paid_topup_bonus_if_needed(db: Session, user: User) -> Optional[ReferralEvent]:
    """À appeler quand l'admin approuve une recharge. Pose le flag ``has_paid_topup`` et crédite le parrain
    s'il existe et que le bonus n'a pas déjà été attribué (idempotent par UniqueConstraint).
    """
    if user is None or user.id is None:
        return None

    fresh = db.get(User, user.id)
    if fresh is None:
        return None

    already_paid = bool(getattr(fresh, "has_paid_topup", False))
    if not already_paid:
        fresh.has_paid_topup = True
        db.add(fresh)
        db.commit()
        db.refresh(fresh)

    if fresh.referred_by_user_id is None:
        return None

    referrer = db.get(User, fresh.referred_by_user_id)
    if referrer is None:
        return None

    bonus_referrer = paid_topup_bonus_units_referrer()
    bonus_referred = paid_topup_bonus_units_referred()
    if bonus_referrer <= 0 and bonus_referred <= 0:
        return None

    event = ReferralEvent(
        referrer_user_id=referrer.id,
        referred_user_id=fresh.id,
        kind="first_paid_topup_approved",
        referrer_bonus_units=bonus_referrer,
        referred_bonus_units=bonus_referred,
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None

    if bonus_referrer > 0:
        credit_credits(db, referrer, bonus_referrer)
    if bonus_referred > 0:
        credit_credits(db, fresh, bonus_referred)
    return event


def referral_stats(db: Session, user: User) -> dict:
    """Stats UI pour `/api/referrals/me`."""
    if user is None or user.id is None:
        return {
            "referral_code": None,
            "share_url": None,
            "referred_count": 0,
            "paid_referred_count": 0,
            "bonus_signup_units": signup_bonus_units(),
            "bonus_paid_units_referrer": paid_topup_bonus_units_referrer(),
            "bonus_paid_units_referred": paid_topup_bonus_units_referred(),
        }

    referred = db.execute(select(User).where(User.referred_by_user_id == user.id)).scalars().all()
    paid_count = sum(1 for u in referred if bool(getattr(u, "has_paid_topup", False)))
    base = (os.getenv("PUBLIC_APP_BASE_URL") or "").strip().rstrip("/")
    share_url = None
    if user.referral_code and base:
        share_url = f"{base}/?ref={user.referral_code}"
    return {
        "referral_code": user.referral_code,
        "share_url": share_url,
        "referred_count": len(referred),
        "paid_referred_count": paid_count,
        "bonus_signup_units": signup_bonus_units(),
        "bonus_paid_units_referrer": paid_topup_bonus_units_referrer(),
        "bonus_paid_units_referred": paid_topup_bonus_units_referred(),
    }
