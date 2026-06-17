"""Сводка для дашборда мини-аппа (текущий месяц)."""
from __future__ import annotations

import calendar
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from .fx import to_rub
from .settings_store import get_setting


def get_dashboard(db: Session) -> dict:
    now = datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _sum(tx_type: str) -> float:
        q = (db.query(func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
             .filter(models.Transaction.type == tx_type,
                     models.Transaction.datetime >= month_start))
        return float(q.scalar() or 0.0)

    spent = _sum("expense")
    income = _sum("income")

    cat_rows = (
        db.query(models.Category.name,
                 func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0).label("s"))
        .join(models.Transaction, models.Transaction.category_id == models.Category.id)
        .filter(models.Transaction.type == "expense",
                models.Transaction.datetime >= month_start)
        .group_by(models.Category.name)
        .order_by(func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0).desc())
        .limit(8).all()
    )
    by_category = [{"name": n, "sum": round(float(s), 2)} for n, s in cat_rows]

    recent = (db.query(models.Transaction)
              .order_by(models.Transaction.datetime.desc()).limit(10).all())
    recent_out = [{
        "id": t.id, "dt": t.datetime.isoformat(), "amount": round(t.amount, 2),
        "currency": t.currency, "merchant": t.merchant or "", "type": t.type,
    } for t in recent]

    accounts = db.query(models.Account).filter(models.Account.archived.is_(False)).all()
    net_worth = sum(to_rub(a.balance, a.currency, db) for a in accounts)

    exp = get_setting(db, "expected_monthly_income")
    expected = float(exp) if exp is not None else (settings.expected_monthly_income or 0.0)
    safe_to_spend = round(max(expected - spent, 0.0), 2)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_left = max(days_in_month - now.day + 1, 1)
    per_day = round(safe_to_spend / days_left, 2)

    return {
        "month": now.strftime("%Y-%m"),
        "spent": round(spent, 2),
        "income": round(income, 2),
        "by_category": by_category,
        "recent": recent_out,
        "net_worth": round(net_worth, 2),
        "safe_to_spend": safe_to_spend,
        "per_day": per_day,
        "days_left": days_left,
        "currency": settings.base_currency,
    }
