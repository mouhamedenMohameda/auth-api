"""Migrations de schéma idempotentes pour l'évolution non-destructive de la DB.

``Base.metadata.create_all()`` crée les nouvelles tables ; ces fonctions ajoutent
les colonnes manquantes aux tables existantes (utile quand on migre depuis une
ancienne DB LectureAI vers la DB partagée).

Pour les nouveaux déploiements, ces fonctions sont essentiellement des no-ops.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


def _has_column(insp, table: str, col: str) -> bool:
    try:
        cols = {c["name"] for c in insp.get_columns(table)}
        return col in cols
    except Exception:
        return False


def _add_column_safe(engine: Engine, table: str, col: str, ddl: str) -> None:
    """Ajoute une colonne si absente. ``ddl`` contient le type SQL et un DEFAULT/NULL."""
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return  # table créée par metadata.create_all
    if _has_column(insp, table, col):
        return
    sql = f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
        logger.info("schema_migrate: added %s.%s", table, col)
    except Exception as e:
        logger.warning("schema_migrate: could not add %s.%s (%s)", table, col, e)


def ensure_credit_schema(engine: Engine) -> None:
    """Ajoute les colonnes credit_balance / credits_expire_at / is_admin / has_paid_topup."""
    _add_column_safe(engine, "users", "credit_balance", "INTEGER NOT NULL DEFAULT 0")
    _add_column_safe(engine, "users", "credits_expire_at", "TIMESTAMP NULL")
    _add_column_safe(engine, "users", "is_admin", "BOOLEAN NOT NULL DEFAULT 0")
    _add_column_safe(engine, "users", "has_paid_topup", "BOOLEAN NOT NULL DEFAULT 0")


def ensure_referrals_schema(engine: Engine) -> None:
    """Ajoute les colonnes referral_code / referred_by_user_id à users."""
    _add_column_safe(engine, "users", "referral_code", "VARCHAR(16) NULL")
    _add_column_safe(engine, "users", "referred_by_user_id", "INTEGER NULL")
