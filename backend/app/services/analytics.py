"""
Аналитика для вкладки «Аналитика»: кэшфлоу по месяцам, сравнение период-к-периоду
по категориям, структура расходов, топ-продавцы, норма сбережений, подписки.
Период: month | quarter | year (текущий), сравнение — с предыдущим таким же.
"""
from __future__ import annotations

from datetime import datetime

from dateutil.relativedelta import relativedelta
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models


def _period_bounds(period: str, now: datetime):
    if period == "year":
        start = datetime(now.year, 1, 1)
        step = relativedelta(years=1)
    elif period == "quarter":
        qm = ((now.month - 1) // 3) * 3 + 1
        start = datetime(now.year, qm, 1)
        step = relativedelta(months=3)
    else:  # month
        start = datetime(now.year, now.month, 1)
        step = relativedelta(months=1)
    return start, start + step, start - step, start  # cur_start, cur_end, prev_start, prev_end


def _sum_by_category(db: Session, start: datetime, end: datetime) -> dict[str, float]:
    rows = (db.query(models.Category.name,
                     func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
            .join(models.Transaction, models.Transaction.category_id == models.Category.id)
            .filter(models.Transaction.type == "expense",
                    models.Transaction.datetime >= start, models.Transaction.datetime < end)
            .group_by(models.Category.name).all())
    return {n: float(s) for n, s in rows}


def _totals(db: Session, start: datetime, end: datetime) -> dict:
    def s(t: str) -> float:
        return float(db.query(func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
                     .filter(models.Transaction.type == t,
                             models.Transaction.datetime >= start,
                             models.Transaction.datetime < end).scalar() or 0.0)
    inc, exp = s("income"), s("expense")
    net = inc - exp
    return {"income": round(inc, 2), "expense": round(exp, 2), "net": round(net, 2),
            "savings_rate": round(net / inc * 100) if inc > 0 else 0}


def cashflow_series(db: Session, months: int) -> list[dict]:
    now = datetime.now()
    cur = datetime(now.year, now.month, 1)
    out = []
    for i in range(months - 1, -1, -1):
        ws = cur - relativedelta(months=i)
        t = _totals(db, ws, ws + relativedelta(months=1))
        out.append({"ym": ws.strftime("%Y-%m"), "income": t["income"],
                    "expense": t["expense"], "net": t["net"]})
    return out


def top_merchants(db: Session, start: datetime, end: datetime, limit: int = 8) -> list[dict]:
    rows = (db.query(models.Transaction.merchant,
                     func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0).label("s"))
            .filter(models.Transaction.type == "expense",
                    models.Transaction.datetime >= start, models.Transaction.datetime < end,
                    models.Transaction.merchant.isnot(None))
            .group_by(models.Transaction.merchant)
            .order_by(func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0).desc())
            .limit(limit).all())
    return [{"merchant": m, "sum": round(float(s), 2)} for m, s in rows if m and s > 0]


def subscriptions(db: Session) -> dict:
    rows = (db.query(models.Recurring)
            .filter(models.Recurring.active.is_(True), models.Recurring.type == "expense").all())
    items, total = [], 0.0
    for r in rows:
        m = r.amount / 12 if r.period == "yearly" else r.amount * 4.33 if r.period == "weekly" else r.amount
        total += m
        items.append({"name": r.name, "amount": round(r.amount), "period": r.period,
                      "next_date": r.next_date.isoformat() if r.next_date else None})
    items.sort(key=lambda x: -x["amount"])
    return {"total": round(total), "items": items}


def analytics_overview(db: Session, period: str = "month") -> dict:
    period = period if period in ("month", "quarter", "year") else "month"
    start, end, prev_start, prev_end = _period_bounds(period, datetime.now())

    cur = _sum_by_category(db, start, end)
    prev = _sum_by_category(db, prev_start, prev_end)
    # год-к-году: тот же период годом ранее
    yoy = _sum_by_category(db, start - relativedelta(years=1), end - relativedelta(years=1))
    compare = []
    for n in set(cur) | set(prev) | set(yoy):
        c, p, y = round(cur.get(n, 0.0), 2), round(prev.get(n, 0.0), 2), round(yoy.get(n, 0.0), 2)
        if c <= 0 and p <= 0 and y <= 0:
            continue
        compare.append({
            "name": n, "cur": c, "prev": p, "delta": round(c - p, 2),
            "delta_pct": round((c - p) / p * 100) if p > 0 else None,
            "yoy": y,
            "yoy_delta_pct": round((c - y) / y * 100) if y > 0 else None,
        })
    compare.sort(key=lambda x: -abs(x["delta"]))

    by_category = sorted(({"name": n, "sum": round(v, 2)} for n, v in cur.items() if v > 0),
                         key=lambda x: -x["sum"])
    return {
        "period": period,
        "totals": _totals(db, start, end),
        "cashflow": cashflow_series(db, 12 if period == "year" else 6),
        "compare": compare[:8],
        "by_category": by_category[:10],
        "top_merchants": top_merchants(db, start, end),
        "subscriptions": subscriptions(db),
    }
