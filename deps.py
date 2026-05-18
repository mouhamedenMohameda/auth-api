from __future__ import annotations

import os
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Header
from sqlalchemy.orm import Session

from admin_sync import email_matches_designated_admin
from credits_wallet import assert_wallet_can_use
from database import get_db
from models import User
from security import decode_token


def auth_required() -> bool:
    return os.getenv("AUTH_REQUIRED", "true").strip().lower() not in ("0", "false", "no")


def get_current_user_optional(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> Optional[User]:
    if authorization is None or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    if not token:
        return None
    payload = decode_token(token)
    if not payload or payload.get("sub") is None:
        return None
    try:
        user_id = int(payload["sub"])
    except (TypeError, ValueError):
        return None
    user = db.get(User, user_id)
    return user


def require_user(
    user: Annotated[Optional[User], Depends(get_current_user_optional)],
) -> Optional[User]:
    if not auth_required():
        return None
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Connexion requise — connecte-toi pour utiliser cet outil.",
        )
    return user


def require_wallet_user(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[Optional[User], Depends(get_current_user_optional)],
) -> Optional[User]:
    """Connexion + portefeuille si AUTH_REQUIRED ; sinon utilisateur optionnel (JWT si présent).

    Important : avec AUTH_REQUIRED=false, l’ancienne chaîne passait par ``require_user`` qui renvoyait
    toujours ``None`` → ``debit_credits`` ne débitait jamais (transcription / cours / export gratuits par erreur).
    Ici on lit le JWT optionnel pour pouvoir facturer un compte connecté même en mode sans garde-fou global.
    """
    if not auth_required():
        return user
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Connexion requise — connecte-toi pour utiliser cet outil.",
        )
    return assert_wallet_can_use(db, user)


def require_admin_user(
    db: Annotated[Session, Depends(get_db)],
    token_user: Annotated[Optional[User], Depends(require_user)],
) -> User:
    """Validateur réservé à l’e-mail identique à `ADMIN_EMAIL` (fichier `.env` du serveur, pas votre copie locale)."""
    if not auth_required():
        raise HTTPException(
            status_code=403,
            detail="L’interface admin JWT exige AUTH_REQUIRED=true sur ce serveur.",
        )
    if token_user is None:
        raise HTTPException(status_code=401, detail="Connexion administrateur requise.")
    u = db.get(User, token_user.id)
    if not u:
        raise HTTPException(status_code=401, detail="Session invalide.")
    if not email_matches_designated_admin(u.email):
        raise HTTPException(
            status_code=403,
            detail="Ce compte n’a pas les droits de validation des recharges (ADMIN_EMAIL sur le serveur).",
        )
    return u
