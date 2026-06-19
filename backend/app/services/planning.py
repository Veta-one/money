"""
Цели, регулярные платежи, прогноз. Питает safe-to-spend и goal discovery.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from math import ceil

from dateutil.relativedelta import relativedelta
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from .fx import to_rub
from .settings_store import get_setting


def expected_monthly_expense(db: Session, months: int = 3) -> float:
    """Средний месячный расход по последним N ПОЛНЫМ календарным месяцам.

    Берём именно полные месяцы (без текущего неполного), чтобы цифра не «плыла»
    каждый день от скользящего окна и не занижалась обрезанным текущим месяцем.
    Учитываются ВСЕ расходы (в т.ч. без категории), чтобы совпадать с реальностью
    и с разбивкой на дашборде. Если полных месяцев меньше N — делим на доступное.
    """
    now = datetime.now()
    cur_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    y, m = cur_start.year, cur_start.month - months
    while m <= 0:
        m += 12
        y -= 1
    window_start = datetime(y, m, 1)
    # самый ранний расход — чтобы не делить на месяцы, которых ещё не было
    earliest = (db.query(func.min(models.Transaction.datetime))
                .filter(models.Transaction.type == "expense").scalar())
    if earliest and earliest > window_start:
        window_start = earliest.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    months_real = max(1, (cur_start.year - window_start.year) * 12
                      + (cur_start.month - window_start.month))
    total = float(db.query(func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
                  .filter(models.Transaction.type == "expense",
                          models.Transaction.datetime >= window_start,
                          models.Transaction.datetime < cur_start).scalar() or 0.0)
    return round(total / months_real, 2)


# совместимость: старое имя → новая корректная реализация
def avg_monthly_expense(db: Session) -> float:
    return expected_monthly_expense(db)


def obligatory_monthly(db: Session) -> float:
    """Сумма активных регулярных РАСХОДОВ в пересчёте на месяц (для safe-to-spend)."""
    total = 0.0
    for r in (db.query(models.Recurring)
              .filter(models.Recurring.active.is_(True), models.Recurring.type == "expense").all()):
        if r.period == "yearly":
            total += r.amount / 12
        elif r.period == "weekly":
            total += r.amount * 4.33
        else:
            total += r.amount
    return round(total, 2)


def category_forecast(db: Session, months: int = 3) -> dict[str, float]:
    """Средние траты по категориям за последние N полных месяцев (без текущего) → прогноз/мес."""
    now = datetime.now()
    cur_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    y, m = cur_start.year, cur_start.month - months
    while m <= 0:
        m += 12
        y -= 1
    window_start = datetime(y, m, 1)
    rows = (db.query(models.Category.name,
                     func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
            .join(models.Transaction, models.Transaction.category_id == models.Category.id)
            .filter(models.Transaction.type == "expense",
                    models.Transaction.datetime >= window_start,
                    models.Transaction.datetime < cur_start)
            .group_by(models.Category.name).all())
    return {n: round(s / months, 2) for n, s in rows if s > 0}


def goals_monthly_plan(db: Session) -> float:
    rows = db.query(models.Goal).filter(models.Goal.status == "active").all()
    return round(sum((g.monthly_plan or 0) for g in rows), 2)


def detect_recurring(db: Session, months: int = 3, max_per_month: float = 1.6, top: int = 8) -> list[dict]:
    """Находит периодические списания (подписки/ЖКХ): продавец в ≥2 месяцах и ~раз в месяц."""
    start = datetime.now() - timedelta(days=months * 31)
    rows = (db.query(models.Transaction.merchant, models.Transaction.datetime,
                     models.Transaction.base_amount_rub)
            .filter(models.Transaction.type == "expense",
                    models.Transaction.datetime >= start,
                    models.Transaction.merchant.isnot(None)).all())
    existing = {(r.name or "").strip().lower() for r in db.query(models.Recurring).all()}
    dismissed = set(json.loads(get_setting(db, "dismissed_recurring") or "[]"))
    agg: dict[str, dict] = {}
    for merch, dt, amt in rows:
        mn = (merch or "").strip()
        if not mn:
            continue
        a = agg.setdefault(mn, {"months": set(), "amounts": []})
        a["months"].add((dt.year, dt.month))
        a["amounts"].append(amt or 0.0)
    cands = []
    for mn, a in agg.items():
        nm, cnt = len(a["months"]), len(a["amounts"])
        name = mn[:60]   # то же имя, что уходит во фронт и в dismissed/existing
        if nm >= 2 and cnt / nm <= max_per_month and name.lower() not in existing and name not in dismissed:
            amts = sorted(a["amounts"])
            median = amts[len(amts) // 2]
            cands.append({"name": name, "amount": round(median), "months": nm})
    cands.sort(key=lambda c: (-c["months"], -c["amount"]))
    return cands[:top]


def goal_view(g: models.Goal, db: Session | None = None) -> dict:
    target = g.target_amount or 0
    acc = db.get(models.Account, g.account_id) if (g.account_id and db is not None) else None
    current = round(to_rub(acc.balance, acc.currency, db)) if acc else (g.current_amount or 0)
    remaining = max(target - current, 0)
    pct = min(round(current / target * 100), 100) if target else 0
    eta = None
    if g.monthly_plan and g.monthly_plan > 0 and remaining > 0:
        eta = (date.today() + relativedelta(months=ceil(remaining / g.monthly_plan))).isoformat()
    return {
        "id": g.id, "name": g.name, "target_amount": target, "current_amount": current,
        "monthly_plan": g.monthly_plan or 0, "currency": g.currency,
        "target_date": g.target_date.isoformat() if g.target_date else None,
        "status": g.status, "remaining": remaining, "pct": pct, "eta": eta,
        "account_id": g.account_id, "account": acc.name if acc else None,
    }


def suggest_goals(db: Session) -> dict:
    from .income import expected_income_monthly
    expected = expected_income_monthly(db)
    spend = expected_monthly_expense(db)
    capacity = max(round(expected - spend), 0)
    monthly = spend or 50000
    suggestions = [{
        "name": "Финансовая подушка",
        "target_amount": round(6 * monthly),
        "monthly_plan": (max(round(capacity * 0.4), 5000) if capacity else 10000),
        "why": "6 месяцев расходов — на случай форс-мажора",
    }]
    if capacity > 0:
        suggestions.append({
            "name": "Накопить за год",
            "target_amount": round(capacity * 12),
            "monthly_plan": capacity,
            "why": f"при темпе ~{capacity} ₽/мес за год",
        })
    return {"capacity": capacity, "monthly_spend": round(spend),
            "expected_income": round(expected), "suggestions": suggestions}
