"""Validation des variables d'environnement au démarrage.

Appelée depuis le ``lifespan`` de FastAPI. Erreurs critiques (config manquante en prod)
provoquent un ``RuntimeError`` ; warnings non bloquants pour les vars optionnelles.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _positive_int(name: str, *, allow_zero: bool = False) -> tuple[bool, str | None]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return True, None  # absent → on laisse le défaut applicatif
    try:
        v = int(raw)
    except ValueError:
        return False, f"{name}={raw!r} : entier attendu."
    if v < 0 or (v == 0 and not allow_zero):
        return False, f"{name}={raw!r} : entier positif {'(ou 0)' if allow_zero else ''}attendu."
    return True, None


def _slowapi_format(name: str) -> tuple[bool, str | None]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return True, None
    # format slowapi : "N/unit" (ex. "120/minute", "5/hour")
    if not re.match(r"^\d+\s*/\s*(second|minute|hour|day)s?$", raw):
        return False, f"{name}={raw!r} : format slowapi attendu (ex. '120/minute')."
    return True, None


def validate_env(*, auth_required: bool) -> None:
    """Valide la config. Lève RuntimeError si une var critique est manquante/invalide."""
    errors: list[str] = []
    warnings: list[str] = []

    if auth_required:
        secret = os.getenv("JWT_SECRET", "").strip()
        if not secret:
            errors.append("JWT_SECRET est obligatoire avec AUTH_REQUIRED=true.")
        elif len(secret) < 16:
            errors.append("JWT_SECRET doit faire au moins 16 caractères.")

        if not os.getenv("ADMIN_EMAIL", "").strip():
            warnings.append("ADMIN_EMAIL non défini — aucun compte ne pourra valider les recharges.")

        origins = os.getenv("ALLOWED_ORIGINS", "*").strip()
        if origins == "*":
            warnings.append(
                "ALLOWED_ORIGINS=* avec AUTH_REQUIRED=true — fixe la liste explicite des domaines en prod."
            )

    if not os.getenv("OPENAI_API_KEY", "").strip() and not os.getenv("GROQ_API_KEY", "").strip():
        warnings.append("Ni OPENAI_API_KEY ni GROQ_API_KEY défini — transcription/génération indisponibles.")

    for var in ("TRANSCRIBE_JOB_MAX_CONCURRENT", "MRU_WALLET_MICRO", "CREDITS_REGISTRATION_BONUS"):
        ok, msg = _positive_int(var, allow_zero=(var == "CREDITS_REGISTRATION_BONUS"))
        if not ok and msg:
            errors.append(msg)

    ok, msg = _slowapi_format("RATE_LIMIT_DEFAULT")
    if not ok and msg:
        errors.append(msg)

    for w in warnings:
        logger.warning("[env] %s", w)

    if errors:
        joined = "\n  - " + "\n  - ".join(errors)
        raise RuntimeError(f"Configuration invalide au démarrage :{joined}")

    logger.info("[env] validation OK (warnings=%d)", len(warnings))
