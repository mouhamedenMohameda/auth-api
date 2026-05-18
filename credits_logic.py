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

    Si ``CREDITS_REGISTRATION_BONUS`` est absent ou vide : aucun bonus (0).
    Sinon : entier interprété comme unités brutes ; ``0`` désactive explicitement le bonus.
    """
    raw = os.getenv("CREDITS_REGISTRATION_BONUS")
    if raw is None or str(raw).strip() == "":
        return 5 * MRU_WALLET_MICRO
    return max(0, int(raw))


def registration_validity_days() -> int:
    return max(1, int(os.getenv("CREDITS_REGISTRATION_VALIDITY_DAYS", "365")))


def topup_approve_extend_days_default() -> int:
    return max(1, int(os.getenv("CREDITS_TOPUP_APPROVE_EXTEND_DAYS", "90")))
