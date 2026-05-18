"""Modèles SQLAlchemy de l'API auth/billing partagée.

Conservé du LectureAI : User (sans champs spécifiques transcription/WhatsApp/Telegram),
CreditTopUpRequest, ReferralEvent.
Ajouté : WalletTransaction (audit S2S des débits/crédits par app consommatrice).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class User(Base):
    """Utilisateur applicatif partagé entre tous les apps (Débloque-moi, LectureAI, etc.).

    Portefeuille (`credit_balance`) :
      Solde en **unités entières** (pas de fraction stockée).
      ``MRU_affiché ≈ credit_balance / MRU_WALLET_MICRO`` (voir ``pricing``).
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    nni: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    whatsapp_phone: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    credit_balance: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        default=0,
        comment=(
            "Solde portefeuille en unités entières. "
            "MRU affiché ≈ credit_balance / MRU_WALLET_MICRO (voir pricing.py)."
        ),
    )
    credits_expire_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )

    # Parrainage : code public unique généré au signup (alphanum sans 0/O/I/1).
    referral_code: Mapped[Optional[str]] = mapped_column(
        String(16), unique=True, index=True, nullable=True
    )
    referred_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    has_paid_topup: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )

    topup_requests: Mapped[list["CreditTopUpRequest"]] = relationship(
        "CreditTopUpRequest", back_populates="user", cascade="all, delete-orphan"
    )


class CreditTopUpRequest(Base):
    __tablename__ = "credit_top_up_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stored_filename: Mapped[str] = mapped_column(String(384), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True, default="pending"
    )
    credits_granted: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    admin_note: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="topup_requests")


class ReferralEvent(Base):
    """Trace d'un bonus parrainage attribué — audit + idempotence.

    ``kind`` :
      - ``signup``                   : petit bonus immédiat à l'inscription.
      - ``first_paid_topup_approved``: gros bonus à la 1ère recharge approuvée.
    """

    __tablename__ = "referral_events"
    __table_args__ = (
        UniqueConstraint("referred_user_id", "kind", name="uq_referral_events_user_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    referred_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    referrer_bonus_units: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    referred_bonus_units: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WalletTransaction(Base):
    """Audit des opérations sur le portefeuille (débits/crédits par app consommatrice).

    Permet de tracer quel app a consommé combien chez quel user — utile pour la facturation
    interne et le suivi par app (Débloque-moi vs LectureAI etc.).
    """

    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Identifiant de l'app consommatrice (ex: "debloquemoi", "lecturai").
    app_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Sens : "debit" (consommation) | "credit" (remboursement / top-up).
    direction: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    # Montant en unités portefeuille (entiers).
    amount_units: Mapped[int] = mapped_column(Integer, nullable=False)
    # Solde APRÈS l'opération (pour pouvoir reconstruire l'historique).
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    # Référence externe optionnelle (ex: ID de réponse Groq, ID interne de l'app).
    external_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    # Métadonnée libre (ex: "ocr", "hint level 3", "topup #42 approved").
    note: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
