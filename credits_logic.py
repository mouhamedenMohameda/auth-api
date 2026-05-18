"""Coûts en crédits (entiers) débités après succès API — surchargeables par variables d'environnement."""

from __future__ import annotations

import os

from pricing import MRU_WALLET_MICRO


def credits_transcribe() -> int:
    """Débit fixe désactivé : le débit suit le coût API en MRU (voir routes transcribe/export). Conservé pour compat refs."""
    return max(0, int(os.getenv("CREDITS_DEBIT_TRANSCRIBE", "0")))


def credits_generate() -> int:
    return max(0, int(os.getenv("CREDITS_DEBIT_GENERATE", "0")))


def credits_export() -> int:
    return max(0, int(os.getenv("CREDITS_DEBIT_EXPORT", "0")))


def registration_bonus_credits() -> int:
    """Bonus d'inscription en unités portefeuille (MRU micro).

    Désactivé par défaut (0) : remplacé par les ``free hints`` (voir
    :func:`registration_free_hints_count`). Si tu veux ré-activer un bonus MRU
    historique, set ``CREDITS_REGISTRATION_BONUS`` à la valeur souhaitée.
    """
    raw = os.getenv("CREDITS_REGISTRATION_BONUS")
    if raw is None or str(raw).strip() == "":
        return 0
    return max(0, int(raw))


def registration_validity_days() -> int:
    return max(1, int(os.getenv("CREDITS_REGISTRATION_VALIDITY_DAYS", "365")))


def registration_free_hints_count() -> int:
    """Nombre de hints gratuits offerts à l'inscription.

    Défaut : 10. Override possible via ``FREE_HINTS_ON_SIGNUP``.
    """
    return max(0, int(os.getenv("FREE_HINTS_ON_SIGNUP", "10")))


def registration_free_hints_validity_hours() -> int:
    """Durée de validité (heures) du lot de free hints offert à l'inscription.

    Défaut : 24h. Override via ``FREE_HINTS_VALIDITY_HOURS``.
    """
    return max(1, int(os.getenv("FREE_HINTS_VALIDITY_HOURS", "24")))


def topup_approve_extend_days_default() -> int:
    return max(1, int(os.getenv("CREDITS_TOPUP_APPROVE_EXTEND_DAYS", "90")))
