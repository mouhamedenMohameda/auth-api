"""Un seul compte admin : e-mail = ADMIN_EMAIL. Synchronisation au démarrage et après inscription."""

from __future__ import annotations

import logging
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from database import SessionLocal
from models import User

logger = logging.getLogger(__name__)


def designated_admin_email_lower() -> str:
    """E-mail admin désignée (`ADMIN_EMAIL` dans `.env` sur **le serveur** qui fait tourner l’API)."""
    return os.getenv("ADMIN_EMAIL", "").strip().lower()


def email_matches_designated_admin(email: str | None) -> bool:
    """Droits admin : même règle partout que la synchro DB (priorité au match e-mail avec `ADMIN_EMAIL`)."""
    ae = designated_admin_email_lower()
    if not ae or email is None:
        return False
    return email.strip().lower() == ae


def apply_designated_admin(session: Session) -> tuple[str, bool]:
    """
    Met à jour `is_admin` pour tous les utilisateurs.
    Retourne (admin_email_normalisé, au_moins_un_compte_correspond).
    """
    admin_email = designated_admin_email_lower()
    users = list(session.execute(select(User)).scalars().all())
    matched = False
    for u in users:
        is_adm = bool(admin_email) and (u.email.strip().lower() == admin_email)
        if is_adm:
            matched = True
        if u.is_admin != is_adm:
            u.is_admin = is_adm
        session.add(u)
    session.commit()
    return admin_email, matched


def sync_designated_admin() -> None:
    """À appeler au démarrage du serveur (logs si configuration incomplète)."""
    db = SessionLocal()
    try:
        admin_email, matched = apply_designated_admin(db)
        if admin_email and not matched:
            logger.warning(
                "ADMIN_EMAIL défini (« %s ») mais aucun compte enregistré avec cet e-mail. "
                "Inscris-toi puis redémarre le serveur pour activer les droits admin.",
                admin_email,
            )
        elif not admin_email:
            logger.warning(
                "ADMIN_EMAIL vide sur ce serveur : définis-le dans `/var/www/.../backend/.env` puis redémarre "
                "(le rsync peut exclure `.env`)."
            )
    except Exception:
        logger.exception("sync_designated_admin a échoué")
        db.rollback()
    finally:
        db.close()
