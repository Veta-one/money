"""
Доходы по источникам. Источник = Recurring(type="income"): Шкулёв, Turnvoice,
пособия жены и т.п. Питает safe-to-spend (ожидаемый доход = сумма источников) и
вкладку «Доходы» (план-факт, разбивка, нуджи продления).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from .fx import to_rub
from .settings_store import get_setting, set_setting


def _norm(s: str | None) -> str:
    return (s or "").lower().replace("ё", "е").strip()


def monthly_rub(rec: models.Recurring, db: Session) -> float:
    """Сумма источника в пересчёте на месяц и в рубли."""
    amt = to_rub(rec.amount or 0.0, rec.currency or "RUB", db)
    if rec.period == "yearly":
        return amt / 12
    if rec.period == "weekly":
        return amt * 4.33
    return amt


def expected_income_monthly(db: Session) -> float:
    """Ожидаемый доход/мес = сумма активных источников. Фолбэк — старая настройка."""
    srcs = (db.query(models.Recurring)
            .filter(models.Recurring.active.is_(True),
                    models.Recurring.type == "income").all())
    if srcs:
        return round(sum(monthly_rub(s, db) for s in srcs), 2)
    val = get_setting(db, "expected_monthly_income")
    return float(val) if val is not None else (settings.expected_monthly_income or 0.0)


def _aliases(db: Session) -> list[dict]:
    raw = get_setting(db, "income_aliases")
    try:
        return json.loads(raw) if raw else []
    except Exception:  # noqa: BLE001
        return []


def learn_income_alias(db: Session, merchant: str | None, note: str | None, rec_id: int) -> None:
    """Запоминает: этот продавец/назначение → этот источник (для авто-привязки впредь)."""
    key = (_norm(merchant) or _norm(note))[:60]
    if not key:
        return
    lst = [a for a in _aliases(db) if a.get("m") != key]
    lst.append({"m": key, "r": rec_id})
    set_setting(db, "income_aliases", json.dumps(lst, ensure_ascii=False))


def attribute_income(db: Session) -> None:
    """Авто-матч непривязанных income-транзакций к источнику: по имени и по выученным алиасам."""
    srcs = db.query(models.Recurring).filter(models.Recurring.type == "income").all()
    valid_ids = {s.id for s in srcs}
    named = [(s.id, _norm(s.name)) for s in srcs if _norm(s.name)]
    aliases = [(a["m"], a["r"]) for a in _aliases(db)
               if a.get("m") and a.get("r") in valid_ids]
    if not named and not aliases:
        return
    txs = (db.query(models.Transaction)
           .filter(models.Transaction.type == "income",
                   models.Transaction.recurring_id.is_(None)).all())
    changed = False
    for t in txs:
        hay = _norm(t.merchant) + " " + _norm(t.note)
        match = None
        for sid, nm in named:
            if nm in hay:
                match = sid
                break
        if match is None:
            for m, r in aliases:
                if m in hay:
                    match = r
                    break
        if match is not None:
            t.recurring_id = match
            changed = True
    if changed:
        db.commit()


def income_overview(db: Session) -> dict:
    now = datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    attribute_income(db)

    rows = (db.query(models.Transaction.recurring_id,
                     func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
            .filter(models.Transaction.type == "income",
                    models.Transaction.datetime >= month_start)
            .group_by(models.Transaction.recurring_id).all())
    fact_by_rec = {rid: float(s) for rid, s in rows}
    fact_total = round(sum(fact_by_rec.values()), 2)
    unattributed = round(fact_by_rec.get(None, 0.0), 2)

    all_income = {s.id: s for s in db.query(models.Recurring)
                  .filter(models.Recurring.type == "income").all()}

    # план-факт по активным источникам
    active = [s for s in all_income.values() if s.active]
    active.sort(key=lambda s: -monthly_rub(s, db))
    sources, plan_total = [], 0.0
    today = date.today()
    soon = today + timedelta(days=30)
    nudges = []
    for s in active:
        plan = monthly_rub(s, db)
        plan_total += plan
        fact = fact_by_rec.get(s.id, 0.0)
        sources.append({
            "id": s.id, "name": s.name, "amount": round(s.amount or 0.0, 2),
            "currency": s.currency, "period": s.period, "owner": s.owner,
            "active": s.active, "plan": round(plan), "fact": round(fact),
            "pct": min(100, round(fact / plan * 100)) if plan else 0,
            "end_date": s.end_date.isoformat() if s.end_date else None,
        })
        if s.end_date and s.end_date <= soon:
            nudges.append({"id": s.id, "name": s.name, "end_date": s.end_date.isoformat()})

    # фактическая разбивка за месяц (включая прочее/нераспознанное)
    breakdown = []
    for rid, amt in fact_by_rec.items():
        if amt <= 0:
            continue
        name = all_income[rid].name if rid in all_income else "Прочее"
        breakdown.append({"name": name, "amount": round(amt, 2)})
    breakdown.sort(key=lambda b: -b["amount"])

    return {
        "plan_total": round(plan_total),
        "fact_total": fact_total,
        "unattributed": unattributed,
        "currency": settings.base_currency,
        "sources": sources,
        "breakdown": breakdown,
        "nudges": nudges,
    }
