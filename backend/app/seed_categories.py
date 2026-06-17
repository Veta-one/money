"""
Заливка дерева категорий из categories.json в БД. Идемпотентно.
Запуск:  python -m app.seed_categories  [путь_к_categories.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .db import Base, SessionLocal, engine
from .models import Category


def seed(path: str = "categories.json") -> None:
    Base.metadata.create_all(bind=engine)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    db = SessionLocal()
    try:
        existing = {c.name for c in db.query(Category).all()}
        added = 0
        for ctype in ("expense", "income", "transfer"):
            for cat in data.get(ctype, []):
                if cat["name"] not in existing:
                    parent = Category(name=cat["name"], type=ctype)
                    db.add(parent)
                    db.flush()
                    existing.add(cat["name"])
                    added += 1
                else:
                    parent = db.query(Category).filter_by(name=cat["name"]).first()
                for child in cat.get("children", []):
                    if child not in existing:
                        db.add(Category(name=child, type=ctype, parent_id=parent.id))
                        existing.add(child)
                        added += 1
        db.commit()
        print(f"Категории залиты. Добавлено новых: {added}")
    finally:
        db.close()


if __name__ == "__main__":
    seed(sys.argv[1] if len(sys.argv) > 1 else "categories.json")
