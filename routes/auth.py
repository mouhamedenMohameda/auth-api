import os
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from credits_logic import registration_bonus_credits, registration_validity_days
from credits_wallet import utc_now, wallet_block_reason
from admin_sync import apply_designated_admin, designated_admin_email_lower, email_matches_designated_admin
from database import get_db
from deps import auth_required, get_current_user_optional
from models import User
from pricing import wallet_units_to_mru_display
from phone_norm import normalize_nni, normalize_whatsapp
from referrals import (
    attach_referrer_and_apply_signup_bonus,
    generate_unique_code,
    normalize_code as normalize_referral_code,
)
from rate_limit import limiter
from security import create_access_token, hash_password, verify_password

router = APIRouter(tags=["auth"])

# Comptes perdus lors d’une perte de base : message de connexion explicite si l’e-mail n’existe plus en base.
# Configurable via env var LEGACY_REREGISTER_EMAILS (séparés par virgule).
def _load_legacy_reregister_emails() -> frozenset[str]:
    raw = os.getenv("LEGACY_REREGISTER_EMAILS", "")
    return frozenset(
        e.strip().lower() for e in raw.split(",") if e.strip()
    )


_LEGACY_REREGISTER_LOGIN_HINT_EMAILS = _load_legacy_reregister_emails()

_LEGACY_REREGISTER_LOGIN_HINT_DETAIL = (
    "Aucun compte avec cette adresse. Passe d’abord par l’onglet « Créer un compte » (même e-mail), puis « Connexion »."
)


def _mask_nni(nni: str) -> str:
    if len(nni) <= 4:
        return "****"
    return "*" * (len(nni) - 4) + nni[-4:]


def _mask_phone(phone: str) -> str:
    d = "".join(c for c in phone if c.isdigit())
    if len(d) <= 4:
        return "****"
    return "***" + d[-4:]


def _user_payload(u: User) -> dict:
    return {
        "email": u.email,
        "nni_masked": _mask_nni(u.nni),
        "whatsapp_masked": _mask_phone(u.whatsapp_phone),
        "is_admin": email_matches_designated_admin(u.email),
    }


class RegisterBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    nni: str
    whatsapp: str
    # Code de parrainage optionnel (alphanum sans 0/O/I/1, 6–16 chars). Validation tolérante :
    # un code invalide est silencieusement ignoré (mieux qu'un 422 confus pour l'utilisateur final).
    referral_code: Optional[str] = Field(default=None, max_length=24)

    @field_validator("nni", mode="before")
    @classmethod
    def nni_norm(cls, v: str) -> str:
        return normalize_nni(str(v).strip())

    @field_validator("whatsapp", mode="before")
    @classmethod
    def wa_norm(cls, v: str) -> str:
        return normalize_whatsapp(str(v).strip())


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class ResetBody(BaseModel):
    """Réinitialisation : vérification par re-saisie e-mail + NNI + WhatsApp (aucun envoi automatique)."""

    email: EmailStr
    nni: str
    whatsapp: str
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("nni", mode="before")
    @classmethod
    def nni_norm(cls, v: str) -> str:
        return normalize_nni(str(v).strip())

    @field_validator("whatsapp", mode="before")
    @classmethod
    def wa_norm(cls, v: str) -> str:
        return normalize_whatsapp(str(v).strip())


@router.get("/auth/config")
def auth_config():
    return {
        "auth_required": auth_required(),
        "designated_admin_configured": bool(designated_admin_email_lower()),
    }


@router.post("/auth/register")
def register(request: Request, body: RegisterBody, db: Session = Depends(get_db)):
    if not auth_required():
        raise HTTPException(
            status_code=403,
            detail="Les inscriptions sont désactivées lorsque AUTH_REQUIRED=false.",
        )
    bonus = registration_bonus_credits()
    user = User(
        email=body.email.strip().lower(),
        password_hash=hash_password(body.password),
        nni=body.nni,
        whatsapp_phone=body.whatsapp,
        credit_balance=bonus,
        credits_expire_at=(utc_now() + timedelta(days=registration_validity_days())) if bonus > 0 else None,
        referral_code=generate_unique_code(db),
    )
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Cet e-mail, ce NNI ou ce numéro WhatsApp est déjà enregistré.",
        ) from None
    apply_designated_admin(db)
    db.refresh(user)
    # Parrainage : tolérant — un code invalide / inconnu / self-référence n'empêche pas l'inscription.
    if body.referral_code and normalize_referral_code(body.referral_code):
        try:
            attach_referrer_and_apply_signup_bonus(db, user, body.referral_code)
            db.refresh(user)
        except Exception:
            db.rollback()
    token = create_access_token(user.id, user.email)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _user_payload(user),
    }


@router.post("/auth/login")
def login(request: Request, body: LoginBody, db: Session = Depends(get_db)):
    if not auth_required():
        raise HTTPException(
            status_code=403,
            detail="La connexion est désactivée lorsque AUTH_REQUIRED=false.",
        )
    email = body.email.strip().lower()
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user and verify_password(body.password, user.password_hash):
        token = create_access_token(user.id, user.email)
        return {
            "access_token": token,
            "token_type": "bearer",
            "user": _user_payload(user),
        }
    if user is None and email in _LEGACY_REREGISTER_LOGIN_HINT_EMAILS:
        raise HTTPException(status_code=401, detail=_LEGACY_REREGISTER_LOGIN_HINT_DETAIL)
    raise HTTPException(status_code=401, detail="E-mail ou mot de passe incorrect.")
    token = create_access_token(user.id, user.email)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _user_payload(user),
    }


@router.get("/auth/me")
def me(
    user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not auth_required():
        return {"authenticated": False, "auth_disabled": True}
    if user is None:
        raise HTTPException(status_code=401, detail="Non authentifié.")
    u = db.get(User, user.id)
    if not u:
        raise HTTPException(status_code=401, detail="Non authentifié.")
    br = wallet_block_reason(u)
    exp = u.credits_expire_at
    return {
        "authenticated": True,
        "user": {
            "email": u.email,
            "nni_masked": _mask_nni(u.nni),
            "whatsapp_masked": _mask_phone(u.whatsapp_phone),
            "is_admin": email_matches_designated_admin(u.email),
            "credit_balance": u.credit_balance,
            "balance_mru": wallet_units_to_mru_display(int(u.credit_balance)),
            "credits_expire_at": exp.isoformat() if exp else None,
            "credits_blocked_reason": br,
            "can_use_paid_features": br is None,
            "referral_code": u.referral_code,
        },
    }


@router.post("/auth/reset-password")
def reset_password(request: Request, body: ResetBody, db: Session = Depends(get_db)):
    """Met à jour le mot de passe si e-mail + NNI + WhatsApp correspondent au compte (pas d’OTP ni WhatsApp automatique)."""
    if not auth_required():
        raise HTTPException(status_code=403, detail="Réinitialisation indisponible.")
    email = body.email.strip().lower()
    user = db.execute(
        select(User).where(
            User.email == email,
            User.nni == body.nni,
            User.whatsapp_phone == body.whatsapp,
        )
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=400,
            detail="Ces informations ne correspondent à aucun compte ou ne sont pas identiques à celles enregistrées.",
        )

    user.password_hash = hash_password(body.new_password)
    db.add(user)
    db.commit()
    return {"message": "Mot de passe mis à jour. Tu peux te connecter avec le nouveau mot de passe."}
