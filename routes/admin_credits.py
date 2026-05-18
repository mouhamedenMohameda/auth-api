from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from credits_logic import topup_approve_extend_days_default
from credits_wallet import utc_now
from database import get_db
from deps import require_admin_user
from referrals import apply_first_paid_topup_bonus_if_needed
from models import CreditTopUpRequest, User
from pricing import (
    MRU_PER_USD,
    MARGIN_MULTIPLIER,
    grant_mru_to_wallet_units,
    mru_signed_to_wallet_units_delta,
    usd_provider_to_billed_mru,
    wallet_units_to_mru_display,
)

router = APIRouter(tags=["admin"])
TOPUP_ROOT = Path(__file__).resolve().parent.parent / "data" / "topups"


class ApproveBody(BaseModel):
    """Une seule option : coût fournisseur USD (→ MRU avec marge) OU montant MRU final OU ancien mode unités."""

    supplier_cost_usd: Optional[float] = Field(default=None, gt=0, lt=10_000_000)
    mru_credit: Optional[float] = Field(default=None, gt=0, lt=1e15)
    credit_amount: Optional[int] = Field(
        default=None,
        gt=0,
        le=1_000_000_000_000,
        description="Ancienne forme : unités portefeuille brutes (éviter pour les nouvelles validations).",
    )
    extend_validity_days: Optional[int] = Field(default=None, ge=1, le=3650)
    admin_note: Optional[str] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def exactly_one_grant_mode(self):
        modes = (
            self.supplier_cost_usd is not None,
            self.mru_credit is not None,
            self.credit_amount is not None,
        )
        if sum(1 for m in modes if m) != 1:
            raise ValueError(
                "Indique soit supplier_cost_usd (coût API en USD avant marge), soit mru_credit (MRU à créditer au client), soit credit_amount (unités brut interne, déconseillé)."
            )
        return self


class ManualWalletBody(BaseModel):
    """Ajustement portefeuille admin (sans demande) — `mru_credit` peut être négatif pour retirer."""

    supplier_cost_usd: Optional[float] = Field(default=None, gt=0, lt=10_000_000)
    mru_credit: Optional[float] = Field(default=None, gt=-1e15, lt=1e15)
    credit_amount: Optional[int] = Field(
        default=None,
        gt=0,
        le=1_000_000_000_000,
        description="Ancienne forme : unités portefeuille brutes (positif uniquement).",
    )
    extend_validity_days: Optional[int] = Field(default=None, ge=1, le=3650)
    admin_note: Optional[str] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def exactly_one_grant_mode_manual(self):
        modes = (
            self.supplier_cost_usd is not None,
            self.mru_credit is not None,
            self.credit_amount is not None,
        )
        if sum(1 for m in modes if m) != 1:
            raise ValueError(
                "Indique soit supplier_cost_usd, soit mru_credit (positif ou négatif), soit credit_amount (unités positives)."
            )
        if self.mru_credit is not None and abs(float(self.mru_credit)) < 1e-12:
            raise ValueError("Le montant MRU ne peut pas être nul (utilise un nombre positif pour créditer, négatif pour retirer).")
        return self


class RejectBody(BaseModel):
    admin_note: Optional[str] = Field(default=None, max_length=500)


def _grant_wallet_units(body: ApproveBody) -> tuple[int, float]:
    """Retourne (unités à ajouter, MRU équivalent affiché)."""
    if body.supplier_cost_usd is not None:
        mru = float(usd_provider_to_billed_mru(body.supplier_cost_usd))
        return grant_mru_to_wallet_units(mru), round(mru, 10)
    if body.mru_credit is not None:
        mru = float(body.mru_credit)
        return grant_mru_to_wallet_units(mru), round(mru, 10)
    assert body.credit_amount is not None
    units = body.credit_amount
    return units, wallet_units_to_mru_display(units)


def _extend_validity_credit_user(usr: User, body: ApproveBody) -> tuple[int, float]:
    """Ajoute au portefeuille et rallonge la date d’expiration. Ne fait pas commit."""
    add_units, mru_nominal = _grant_wallet_units(body)
    extend_days = body.extend_validity_days
    if extend_days is None:
        extend_days = topup_approve_extend_days_default()
    now = utc_now()
    old_exp = usr.credits_expire_at
    if old_exp is not None:
        oe = old_exp if old_exp.tzinfo else old_exp.replace(tzinfo=timezone.utc)
        base = oe if oe > now else now
    else:
        base = now
    if getattr(base, "tzinfo", None) is None:
        base = base.replace(tzinfo=timezone.utc)
    usr.credit_balance += add_units
    usr.credits_expire_at = base + timedelta(days=extend_days)
    return add_units, mru_nominal


def _grant_wallet_units_manual(body: ManualWalletBody) -> tuple[int, float]:
    """Retourne (delta d’unités portefeuille, MRU équivalent signé pour l’affichage)."""
    if body.supplier_cost_usd is not None:
        mru = float(usd_provider_to_billed_mru(body.supplier_cost_usd))
        return grant_mru_to_wallet_units(mru), round(mru, 10)
    if body.mru_credit is not None:
        mru = float(body.mru_credit)
        return mru_signed_to_wallet_units_delta(mru), round(mru, 10)
    assert body.credit_amount is not None
    units = body.credit_amount
    return units, wallet_units_to_mru_display(units)


def _apply_manual_wallet_delta(usr: User, body: ManualWalletBody) -> tuple[int, float]:
    delta_units, mru_nominal = _grant_wallet_units_manual(body)
    if usr.credit_balance + delta_units < 0:
        raise HTTPException(
            status_code=400,
            detail="Solde insuffisant pour ce retrait — réduis le montant MRU retiré.",
        )
    extend_days = body.extend_validity_days
    now = utc_now()
    if delta_units > 0:
        if extend_days is None:
            extend_days = topup_approve_extend_days_default()
        old_exp = usr.credits_expire_at
        if old_exp is not None:
            oe = old_exp if old_exp.tzinfo else old_exp.replace(tzinfo=timezone.utc)
            base = oe if oe > now else now
        else:
            base = now
        if getattr(base, "tzinfo", None) is None:
            base = base.replace(tzinfo=timezone.utc)
        usr.credits_expire_at = base + timedelta(days=extend_days)
    usr.credit_balance += delta_units
    return delta_units, mru_nominal


def _grant_response_payload(usr: User, *, add_units: int, mru_nominal: float) -> dict:
    return {
        "ok": True,
        "user_id": usr.id,
        "credit_balance": usr.credit_balance,
        "balance_mru_approx": wallet_units_to_mru_display(usr.credit_balance),
        "wallet_units_added": add_units,
        "mru_credited_approx": round(mru_nominal, 8),
        "mru_per_usd": MRU_PER_USD,
        "customer_margin_multiplier": MARGIN_MULTIPLIER,
        "credits_expire_at": usr.credits_expire_at.isoformat() if usr.credits_expire_at else None,
    }


def _admin_user_summary(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "is_admin": bool(u.is_admin),
        "credit_balance": u.credit_balance,
        "balance_mru_approx": wallet_units_to_mru_display(u.credit_balance),
        "credits_expire_at": u.credits_expire_at.isoformat() if u.credits_expire_at else None,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


@router.get("/admin/users")
def admin_list_users(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    q: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(require_admin_user),
):
    """Liste paginée de tous les comptes ; ``q`` (optionnel) filtre par sous-chaîne d’e-mail (min. 2 caractères)."""
    needle = "".join(ch for ch in (q or "").strip().lower() if ch.isprintable()).strip()
    if len(needle) > 120:
        needle = needle[:120]

    filters = []
    if len(needle) >= 2:
        filters.append(func.instr(func.lower(User.email), needle) > 0)

    count_stmt = select(func.count(User.id))
    if filters:
        count_stmt = count_stmt.where(*filters)
    total = int(db.scalar(count_stmt) or 0)

    list_stmt = select(User).order_by(User.email.asc()).offset(offset).limit(limit)
    if filters:
        list_stmt = list_stmt.where(*filters)
    rows = db.execute(list_stmt).scalars().all()

    return {
        "users": [_admin_user_summary(u) for u in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/admin/users/search")
def admin_search_users(
    q: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(require_admin_user),
):
    """Recherche d’utilisateurs par sous-chaîne d’e-mail (min. 2 caractères)."""
    needle = "".join(ch for ch in (q or "").strip().lower() if ch.isprintable()).strip()
    if len(needle) < 2:
        return {"users": []}
    if len(needle) > 120:
        needle = needle[:120]
    stmt = (
        select(User)
        .where(func.instr(func.lower(User.email), needle) > 0)
        .order_by(User.email.asc())
        .limit(30)
    )
    rows = db.execute(stmt).scalars().all()
    return {
        "users": [
            {
                "id": u.id,
                "email": u.email,
                "is_admin": bool(u.is_admin),
                "credit_balance": u.credit_balance,
                "balance_mru_approx": wallet_units_to_mru_display(u.credit_balance),
            }
            for u in rows
        ]
    }


@router.post("/admin/users/{user_id}/grant-wallet")
def admin_grant_wallet_manual(
    user_id: int,
    body: ManualWalletBody,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin_user),
):
    """Ajoute ou retire des MRU (montant MRU négatif) sans demande de recharge."""
    usr = db.get(User, user_id)
    if not usr:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    # Évite de créditer par erreur le compte utilisé pour cette session admin
    if usr.id == admin.id:
        raise HTTPException(status_code=400, detail="Utilise ce flux pour un utilisateur qui n’est pas le compte admin courant.")

    add_units, mru_nominal = _apply_manual_wallet_delta(usr, body)

    db.add(usr)

    # NB: les notifications utilisateur (table UserNotification) sont gérées dans
    # l'app consommatrice côté front-end. auth-api se contente du débit/crédit.
    db.commit()
    db.refresh(usr)
    return _grant_response_payload(usr, add_units=add_units, mru_nominal=mru_nominal)


@router.get("/admin/credit-topups")
def list_topups(
    status: str = "pending",
    db: Session = Depends(get_db),
    _: User = Depends(require_admin_user),
):
    q = select(CreditTopUpRequest).options(joinedload(CreditTopUpRequest.user))
    if status and status != "all":
        q = q.where(CreditTopUpRequest.status == status)
    q = q.order_by(CreditTopUpRequest.created_at.desc()).limit(100)
    rows = db.execute(q).unique().scalars().all()
    out = []
    for r in rows:
        gr = r.credits_granted
        out.append(
            {
                "id": r.id,
                "user_id": r.user_id,
                "user_email": r.user.email if r.user else "",
                "status": r.status,
                "original_filename": r.original_filename,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
                "credits_granted": gr,
                "granted_mru_approx": wallet_units_to_mru_display(gr) if gr is not None else None,
                "admin_note": r.admin_note,
            }
        )
    return {"requests": out}


@router.get("/admin/credit-topups/{request_id}/proof")
def download_proof(
    request_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin_user),
):
    row = db.get(CreditTopUpRequest, request_id)
    if not row:
        raise HTTPException(status_code=404, detail="Demande introuvable.")
    path = TOPUP_ROOT / row.stored_filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Fichier introuvable.")
    fname = row.original_filename or "preuve-virement.png"
    return FileResponse(
        path,
        filename=fname,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{fname}"'},
    )


@router.post("/admin/credit-topups/{request_id}/approve")
def approve_topup(
    request_id: int,
    body: ApproveBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin_user),
):
    row = db.get(CreditTopUpRequest, request_id)
    if not row or row.status != "pending":
        raise HTTPException(status_code=400, detail="Demande introuvable ou déjà traitée.")
    usr = db.get(User, row.user_id)
    if not usr:
        raise HTTPException(status_code=400, detail="Utilisateur introuvable.")
    add_units, mru_nominal = _extend_validity_credit_user(usr, body)

    row.status = "approved"
    row.credits_granted = add_units
    row.reviewed_at = utc_now()
    row.admin_note = body.admin_note
    db.add(usr)
    db.add(row)

    # Notification utilisateur déplacée vers l'app front (auth-api standalone).

    db.commit()
    db.refresh(usr)
    # Bonus parrainage à la 1ère recharge approuvée (idempotent via UniqueConstraint sur referral_events).
    # Best-effort : un échec ici ne doit pas remonter en erreur d'approbation côté admin.
    try:
        apply_first_paid_topup_bonus_if_needed(db, usr)
    except Exception:
        db.rollback()
    return _grant_response_payload(usr, add_units=add_units, mru_nominal=mru_nominal)


@router.post("/admin/credit-topups/{request_id}/reject")
def reject_topup(
    request_id: int,
    body: RejectBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin_user),
):
    row = db.get(CreditTopUpRequest, request_id)
    if not row or row.status != "pending":
        raise HTTPException(status_code=400, detail="Demande introuvable ou déjà traitée.")
    row.status = "rejected"
    row.reviewed_at = utc_now()
    row.admin_note = body.admin_note
    db.add(row)
    db.commit()
    return {"ok": True}


# NB: l'endpoint /admin/transcription-loyalty/reset-all a été retiré de auth-api
# (spécifique à LectureAI). Si un jour on veut un /admin/wallet-transactions, on
# l'ajoute ici, basé sur la table WalletTransaction.
