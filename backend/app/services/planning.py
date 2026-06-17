"""
Цели, регулярные платежи, прогноз. Питает safe-to-spend и goal discovery.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from math import ceil

from dateutil.relativedelta import relativedelta
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from .settings_store import get_setting


def avg_monthly_expense(db: Session) -> float:
    since = datetime.now() - timedelta(days=90)
    total = float(db.query(func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
                  .filter(models.Transaction.type == "expense",
                          models.Transaction.datetime >= since).scalar() or 0.0)
    return round(total / 3, 2)


def obligatory_monthly(db: Session) -> float:
    """Сумма активных регулярных РАСХОДОВ в месяц (для safe-to-spend)."""
    total = 0.0
    for r in (db.query(models.Recurring)
              .filter(models.Recurring.active.is_(True), models.Recurring.type == "expense").all()):
        total += r.amount if r.period == "monthly" else r.amount * 4.33
    return round(total, 2)


def goals_monthly_plan(db: Session) -> float:
    rows = db.query(models.Goal).filter(models.Goal.status == "active").all()
    return round(sum((g.monthly_plan or 0) for g in rows), 2)


def goal_view(g: models.Goal) -> dict:
    target = g.target_amount or 0
    current = g.current_amount or 0
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
    }


def suggest_goals(db: Session) -> dict:
    expected = float(get_setting(db, "expected_monthly_income") or 0)
    spend = avg_monthly_expense(db)
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
    return {"capacity": capacity, "monthly_spend": round(spend), "suggestions": suggestions}
