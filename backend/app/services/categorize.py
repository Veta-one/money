"""
Категоризация трат: правила (CategoryRule) → Gemini → порог уверенности.
Учится на правках пользователя (learn_rule).
"""
from __future__ import annotations

import json
import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from .llm import gemini


def category_names(db: Session, types=("expense",)) -> list[str]:
    rows = (db.query(models.Category.name)
            .filter(models.Category.type.in_(types), models.Category.archived.is_(False)).all())
    return [n for (n,) in rows]


def category_by_name(db: Session, name: str | None):
    if not name:
        return None
    return (db.query(models.Category)
            .filter(func.lower(models.Category.name) == name.strip().lower()).first())


def rule_category_id(db: Session, inn: str | None, text: str) -> int | None:
    text_l = (text or "").lower()
    for r in db.query(models.CategoryRule).all():
        if r.match_inn and inn and r.match_inn == inn:
            if not r.match_item_pattern or r.match_item_pattern.lower() in text_l:
                return r.category_id
        elif r.match_item_pattern and not r.match_inn and r.match_item_pattern.lower() in text_l:
            return r.category_id
    return None


def learn_rule(db: Session, category_id: int, inn: str | None = None, pattern: str | None = None) -> None:
    if not (inn or pattern):
        return
    db.add(models.CategoryRule(
        match_inn=inn or None,
        match_item_pattern=(pattern or None) and pattern.strip().lower()[:120],
        category_id=category_id, confidence=1.0, hits=1, auto=False))
    db.commit()


def _json_from(raw: str):
    m = re.search(r"(\[.*\]|\{.*\})", raw or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


async def classify_texts(texts: list[str], cats: list[str]) -> list[str | None]:
    """Возвращает список названий категорий (или None) по позициям texts."""
    if not texts:
        return []
    prompt = (
        "Ты — категоризатор личных трат. Отнеси каждую позицию к ОДНОЙ категории из списка.\n"
        f"Категории: {', '.join(cats)}.\n"
        "Верни СТРОГО JSON-массив строк (по одной категории на позицию, в том же порядке). "
        "Если позиция совсем непонятна — верни null.\n\n"
        + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    )
    arr = _json_from(await gemini.text(prompt))
    valid = {c.lower(): c for c in cats}
    out: list[str | None] = []
    for x in (arr or []):
        out.append(valid.get(x.lower()) if isinstance(x, str) else None)
    return (out + [None] * len(texts))[:len(texts)]


async def categorize_items(db: Session, items: list[dict], inn: str | None) -> list[dict]:
    cats = category_names(db, ("expense",))
    result: list[dict | None] = [None] * len(items)
    llm_idx, llm_text = [], []
    for i, it in enumerate(items):
        cid = rule_category_id(db, inn, it["name"])
        if cid:
            result[i] = {"category_id": cid, "needs_review": False}
        else:
            llm_idx.append(i)
            llm_text.append(it["name"])
    if llm_text:
        names = await classify_texts(llm_text, cats)
        for idx, name in zip(llm_idx, names):
            cobj = category_by_name(db, name)
            result[idx] = {"category_id": cobj.id if cobj else None, "needs_review": cobj is None}
    return [r or {"category_id": None, "needs_review": True} for r in result]


async def categorize_one(db: Session, description: str, inn: str | None = None,
                         types=("expense",)) -> tuple[int | None, bool]:
    cid = rule_category_id(db, inn, description)
    if cid:
        return cid, False
    names = await classify_texts([description], category_names(db, types))
    cobj = category_by_name(db, names[0] if names else None)
    return (cobj.id if cobj else None), (cobj is None)
