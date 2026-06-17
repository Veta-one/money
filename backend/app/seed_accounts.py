"""Заводит счета пользователя (идемпотентно). Запуск: python -m app.seed_accounts"""
from __future__ import annotations

from .db import Base, SessionLocal, engine
from .models import Account

ACCOUNTS = [
    {"name": "Райффайзен", "type": "card", "currency": "RUB", "owner": "me"},
    {"name": "Наличные", "type": "cash", "currency": "RUB", "owner": "me"},
    {"name": "Крипта", "type": "crypto", "currency": "USD", "owner": "me", "balance": 8200.0},
    {"name": "Сбер (жена)", "type": "external", "currency": "RUB", "owner": "wife", "is_external": True},
    {"name": "Вклад (жена)", "type": "deposit", "currency": "RUB", "owner": "wife"},
]


def seed() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        existing = {a.name for a in db.query(Account).all()}
        added = 0
        for a in ACCOUNTS:
            if a["name"] not in existing:
                db.add(Account(**a))
                added += 1
        db.commit()
        print(f"Счета: добавлено {added}")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
