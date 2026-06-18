"""
Бюджет по категориям: месячный лимит (свой `Budget` или авто-прогноз
`category_forecast`), план-факт за текущий месяц + свод (доход − расходы − цели).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from .. import models
from .income import expected_income_monthly
from .planning import category_forecast, goals_monthly_plan


def budget_overview(db: Session) -> dict:
    now = datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    fc = category_forecast(db)                       # {имя категории: прогноз/мес}
    manual = {b.category_id: b.amount for b in db.query(models.Budget).all()}

    rows = (db.query(models.Category.id, models.Category.name,
                     func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
            .outerjoin(models.Transaction, and_(
                models.Transaction.category_id == models.Category.id,
                models.Transaction.type == "expense",
                models.Transaction.datetime >= month_start))
            .filter(models.Category.type == "expense", models.Category.archived.is_(False))
            .group_by(models.Category.id, models.Category.name).all())

    items, total_budget, total_spent = [], 0.0, 0.0
    for cid, name, s in rows:
        spent = float(s or 0.0)
        m = manual.get(cid)
        budget = m if m is not None else float(fc.get(name, 0.0))
        if spent <= 0 and budget <= 0:
            continue
        total_budget += budget
        total_spent += spent
        items.append({
            "id": cid, "name": name, "spent": round(spent, 2), "budget": round(budget),
            "manual": m is not None,
            "pct": min(150, round(spent / budget * 100)) if budget > 0 else 0,
            "over": budget > 0 and spent > budget,
        })
    items.sort(key=lambda x: -x["spent"])

    expected = expected_income_monthly(db)
    goals = goals_monthly_plan(db)
    return {
        "month": now.strftime("%Y-%m"),
        "items": items,
        "total_budget": round(total_budget),
        "total_spent": round(total_spent),
        "expected_income": round(expected),
        "goals_plan": round(goals),
        "proficit": round(expected - total_budget - goals),
    }
