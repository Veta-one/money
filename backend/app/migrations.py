"""
Лёгкие идемпотентные миграции схемы (ADD COLUMN / CREATE INDEX).

Запускаются на старте после Base.metadata.create_all(). create_all() создаёт
НОВЫЕ таблицы, но не меняет существующие — поэтому добавление колонок к живой БД
делаем здесь. Для одного пользователя на SQLite этого достаточно; деструктивные
изменения (если когда-нибудь понадобятся) — через Alembic.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger("money")


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {r[1] for r in rows}


def _add_column(conn, table: str, column: str, ddl: str) -> None:
    """ADD COLUMN, если её ещё нет (table/column/ddl — внутренние константы)."""
    if column not in _existing_columns(conn, table):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
        log.info("migration: %s.%s добавлена", table, column)


def run_migrations(engine: Engine) -> None:
    with engine.begin() as conn:
        # --- Фаза B: доходы по источникам ---
        _add_column(conn, "recurring", "owner", "owner VARCHAR(16) DEFAULT 'me'")
        _add_column(conn, "transactions", "recurring_id", "recurring_id INTEGER")

        # индексы под выборки операций/доходов (datetime уже индексирован моделью)
        for name, ddl in (
            ("ix_tx_type", "CREATE INDEX IF NOT EXISTS ix_tx_type ON transactions(type)"),
            ("ix_tx_category", "CREATE INDEX IF NOT EXISTS ix_tx_category ON transactions(category_id)"),
            ("ix_tx_recurring", "CREATE INDEX IF NOT EXISTS ix_tx_recurring ON transactions(recurring_id)"),
        ):
            conn.execute(text(ddl))
