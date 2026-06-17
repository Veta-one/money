"""Настройки key-value в БД (редактируются из мини-аппа)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .. import models


def get_setting(db: Session, key: str, default=None):
    row = db.query(models.Setting).filter_by(key=key).first()
    return row.value if row else default


def set_setting(db: Session, key: str, value) -> None:
    row = db.query(models.Setting).filter_by(key=key).first()
    if row:
        row.value = str(value)
    else:
        db.add(models.Setting(key=key, value=str(value)))
    db.commit()
