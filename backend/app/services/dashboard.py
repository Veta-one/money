"""Сводка для дашборда мини-аппа (текущий месяц)."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from .analytics import _sum_by_category
from .fx import compute_net_worth
from .income import expected_income_monthly
from .planning import category_forecast, goals_monthly_plan, obligatory_monthly
from .trends import category_sparkline


def needs_review(t) -> bool:
    """Операция «на разбор»:
    - status=needs_review;
    - расход/доход без категории;
    - перевод без второго счёта И без debt-связи (СБП по телефону, переводы
      жене, в долг — пока не классифицированы)."""
    if t.status == "needs_review":
        return True
    if t.type in ("expense", "income") and not t.category_id:
        return True
    if t.type == "transfer" and not t.counterparty_account_id:
        return True
    return False


_REVIEW_FILTER = or_(
    models.Transaction.status == "needs_review",
    and_(models.Transaction.type.in_(("expense", "income")),
         models.Transaction.category_id.is_(None)),
    and_(models.Transaction.type == "transfer",
         models.Transaction.counterparty_account_id.is_(None)),
)


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

    prev_start = month_start - relativedelta(months=1)

    def _sum_between(tx_type: str, start, end) -> float:
        return float(db.query(func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
                     .filter(models.Transaction.type == tx_type,
                             models.Transaction.datetime >= start,
                             models.Transaction.datetime < end).scalar() or 0.0)

    spent_prev = _sum_between("expense", prev_start, month_start)
    income_prev = _sum_between("income", prev_start, month_start)
    review_count = int(db.query(func.count(models.Transaction.id))
                       .filter(_REVIEW_FILTER).scalar() or 0)
    prev_cat = _sum_by_category(db, prev_start, month_start)

    cat_rows = (
        db.query(models.Category.id, models.Category.name,
                 func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0).label("s"))
        .join(models.Transaction, models.Transaction.category_id == models.Category.id)
        .filter(models.Transaction.type == "expense",
                models.Transaction.datetime >= month_start)
        .group_by(models.Category.id, models.Category.name)
        .order_by(func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0).desc())
        .limit(8).all()
    )
    by_category = [{"id": cid, "name": n, "sum": round(float(s), 2)} for cid, n, s in cat_rows]
    fc = category_forecast(db)
    for c in by_category:
        c["expected"] = fc.get(c["name"], 0.0)
        c["prev"] = round(prev_cat.get(c["name"], 0.0), 2)
        c["sparkline"] = category_sparkline(db, c["id"], months=6)
    forecast_total = round(sum(fc.values()), 2)

    recent = (db.query(models.Transaction)
              .order_by(models.Transaction.datetime.desc()).limit(12).all())

    def _cn(cid):
        c = db.get(models.Category, cid) if cid else None
        return c.name if c else None

    recent_out = [{
        "id": t.id, "dt": t.datetime.isoformat(), "amount": round(t.amount, 2),
        "currency": t.currency, "base_rub": round(t.base_amount_rub or 0.0, 2),
        "merchant": t.merchant or "", "type": t.type,
        "category": _cn(t.category_id), "review": needs_review(t),
    } for t in recent]

    net_worth = compute_net_worth(db)
    snap = (db.query(models.NetWorthSnapshot)
            .filter(models.NetWorthSnapshot.date <= date.today() - timedelta(days=20))
            .order_by(models.NetWorthSnapshot.date.desc()).first())
    nw_delta = round(net_worth - snap.total_rub, 2) if snap else None

    expected = expected_income_monthly(db)
    obligatory = obligatory_monthly(db)
    goals_plan = goals_monthly_plan(db)
    safe_to_spend = round(max(expected - spent - obligatory - goals_plan, 0.0), 2)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_left = max(days_in_month - now.day + 1, 1)
    per_day = round(safe_to_spend / days_left, 2)

    return {
        "month": now.strftime("%Y-%m"),
        "spent": round(spent, 2),
        "income": round(income, 2),
        "spent_prev": round(spent_prev, 2),
        "income_prev": round(income_prev, 2),
        "saved": round(income - spent, 2),
        "saved_prev": round(income_prev - spent_prev, 2),
        "needs_review": review_count,
        "by_category": by_category,
        "recent": recent_out,
        "net_worth": round(net_worth, 2),
        "net_worth_delta": nw_delta,
        "safe_to_spend": safe_to_spend,
        "per_day": per_day,
        "days_left": days_left,
        "expected_income": round(expected, 2),
        "obligatory": obligatory,
        "goals_plan": goals_plan,
        "forecast_total": forecast_total,
        "currency": settings.base_currency,
    }
