"""Тренды для графиков: траты по месяцам + снимки капитала (net worth) + heatmap по дням."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func

from .. import models
from ..db import SessionLocal
from .fx import compute_net_worth

_MONTHS = ["янв", "фев", "мар", "апр", "май", "июн",
           "июл", "авг", "сен", "окт", "ноя", "дек"]


def monthly_spending(db, months: int = 6) -> list[dict]:
    now = datetime.now()
    keys: list[tuple[int, int]] = []
    y, m = now.year, now.month
    for _ in range(months):
        keys.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    keys.reverse()
    start = datetime(keys[0][0], keys[0][1], 1)
    rows = (db.query(models.Transaction.datetime, models.Transaction.base_amount_rub)
            .filter(models.Transaction.type == "expense",
                    models.Transaction.datetime >= start).all())
    buckets = {k: 0.0 for k in keys}
    for dt, amt in rows:
        k = (dt.year, dt.month)
        if k in buckets:
            buckets[k] += amt or 0.0
    return [{"label": _MONTHS[k[1] - 1], "spent": round(buckets[k], 2)} for k in keys]


def take_networth_snapshot(db) -> float:
    total = compute_net_worth(db)
    today = date.today()
    row = db.query(models.NetWorthSnapshot).filter_by(date=today).first()
    if row:
        row.total_rub = total
    else:
        db.add(models.NetWorthSnapshot(date=today, total_rub=total))
    db.commit()
    return total


def networth_series(db, limit: int = 30) -> list[dict]:
    rows = (db.query(models.NetWorthSnapshot)
            .order_by(models.NetWorthSnapshot.date.desc()).limit(limit).all())
    return [{"date": r.date.isoformat(), "total": round(r.total_rub, 2)} for r in reversed(rows)]


def daily_spending(db, days: int = 365) -> list[dict]:
    """Сумма расходов по календарному дню за последние `days` дней.
    Возвращает массив {date, spent} только для дней с тратами (фронт сам добавит пустые ячейки)."""
    today = date.today()
    start = datetime.combine(today - timedelta(days=days - 1), datetime.min.time())
    rows = (db.query(func.date(models.Transaction.datetime).label("d"),
                     func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0).label("s"))
            .filter(models.Transaction.type == "expense",
                    models.Transaction.datetime >= start)
            .group_by("d").all())
    return [{"date": str(d), "spent": round(float(s), 2)} for d, s in rows if s and float(s) > 0]


def category_sparkline(db, category_id: int, months: int = 6) -> list[float]:
    """Сумма трат категории по месяцам, возвращает массив из `months` чисел (старый→новый)."""
    now = datetime.now()
    keys: list[tuple[int, int]] = []
    y, m = now.year, now.month
    for _ in range(months):
        keys.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    keys.reverse()
    start = datetime(keys[0][0], keys[0][1], 1)
    rows = (db.query(models.Transaction.datetime, models.Transaction.base_amount_rub)
            .filter(models.Transaction.type == "expense",
                    models.Transaction.category_id == category_id,
                    models.Transaction.datetime >= start).all())
    buckets = {k: 0.0 for k in keys}
    for dt, amt in rows:
        k = (dt.year, dt.month)
        if k in buckets:
            buckets[k] += amt or 0.0
    return [round(buckets[k], 2) for k in keys]


def snapshot_job() -> None:
    """Для планировщика — снимок капитала раз в день."""
    db = SessionLocal()
    try:
        take_networth_snapshot(db)
    finally:
        db.close()
