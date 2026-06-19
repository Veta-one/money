"""
Капитал 2.0: линия net worth по снимкам + разложение прироста за период на
сбережения (доход−расход), эффект курса $ и прочее (крипта/правки балансов).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from math import ceil

from dateutil.relativedelta import relativedelta
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from .fx import compute_net_worth, get_usd_rub, to_rub
from .income import expected_income_monthly
from .planning import avg_monthly_expense
from .settings_store import get_setting
from .trends import networth_series

_USD_LIKE = {"USD", "USDT", "USDC", "$"}


def _sum_tx(db: Session, tx_type: str, since: datetime) -> float:
    return float(db.query(func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
                 .filter(models.Transaction.type == tx_type,
                         models.Transaction.datetime >= since).scalar() or 0.0)


def _base_snapshot(db: Session, period: str):
    """Snapshot, от которого считается дельта. period: day|week|month|year."""
    today = date.today()
    if period == "day":
        cutoff = today
    elif period == "week":
        cutoff = today - timedelta(days=7)
    elif period == "year":
        cutoff = today.replace(month=1, day=1)
    else:  # month — по умолчанию
        cutoff = today.replace(day=1)
    q = db.query(models.NetWorthSnapshot)
    # для "day" хочется именно сегодняшний снимок (start-of-day), для остальных —
    # последний на/до cutoff (т.к. снимка ровно на 1-е число может не быть).
    if period == "day":
        base = q.filter(models.NetWorthSnapshot.date == today).first()
    else:
        base = (q.filter(models.NetWorthSnapshot.date <= cutoff)
                .order_by(models.NetWorthSnapshot.date.desc()).first())
    if base is None:
        base = q.order_by(models.NetWorthSnapshot.date.asc()).first()
    return base


def capital_overview(db: Session, period: str = "day") -> dict:
    if period not in ("day", "week", "month", "year"):
        period = "day"
    now = datetime.now()
    today = now.date()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    net_now = compute_net_worth(db)
    usd_now = get_usd_rub(db)
    series = networth_series(db, 60)

    base = _base_snapshot(db, period)
    delta = delta_days = savings = fx = other = None
    if base is not None:
        since = datetime.combine(base.date, datetime.min.time())
        delta = round(net_now - base.total_rub, 2)
        delta_days = (today - base.date).days
        savings = round(_sum_tx(db, "income", since) - _sum_tx(db, "expense", since), 2)
        rate_base = (db.query(models.FxRate)
                     .filter(models.FxRate.currency == "USD", models.FxRate.date <= base.date)
                     .order_by(models.FxRate.date.desc()).first())
        if rate_base:
            usd_bal = sum(a.balance for a in db.query(models.Account)
                          .filter(models.Account.archived.is_(False)).all()
                          if (a.currency or "").upper() in _USD_LIKE)
            fx = round(usd_bal * (usd_now - rate_base.rate_rub), 2)
        other = round(delta - savings - (fx or 0.0), 2)
    from_month_start = base is not None and base.date <= month_start.date()

    # распределение капитала (валюта/тип) + подушка безопасности (мес расходов)
    accounts = db.query(models.Account).filter(models.Account.archived.is_(False)).all()
    by_cur, by_type, liquid = {}, {}, 0.0
    for a in accounts:
        rub = to_rub(a.balance or 0.0, a.currency, db)
        cur = (a.currency or "RUB").upper()
        cur = "USD" if cur in _USD_LIKE else cur
        by_cur[cur] = by_cur.get(cur, 0.0) + rub
        by_type[a.type or "other"] = by_type.get(a.type or "other", 0.0) + rub
        liquid += rub                                # все счета — ликвидная часть подушки
    avg_exp = avg_monthly_expense(db)
    months = round(liquid / avg_exp, 1) if avg_exp > 0 else None

    # цель капитала + прогноз + «сначала заплати себе» (отложено в этом месяце)
    target = float(get_setting(db, "networth_target") or 0)
    monthly_save = round(expected_income_monthly(db) - avg_exp)
    saved_month = round(_sum_tx(db, "income", month_start) - _sum_tx(db, "expense", month_start))
    eta_months = eta_date = None
    if target > net_now and monthly_save > 0:
        eta_months = ceil((target - net_now) / monthly_save)
        eta_date = (date.today() + relativedelta(months=eta_months)).isoformat()

    return {
        "net_worth": net_now, "usd_rate": round(usd_now, 2),
        "series": series, "delta": delta, "delta_days": delta_days,
        "from_month_start": from_month_start, "period": period,
        "period_from": base.date.isoformat() if base else None,
        "savings": savings, "fx": fx, "other": other,
        "allocation_currency": [{"name": k, "sum": round(v)} for k, v in
                                sorted(by_cur.items(), key=lambda x: -x[1]) if v > 0],
        "allocation_type": [{"name": k, "sum": round(v)} for k, v in
                            sorted(by_type.items(), key=lambda x: -x[1]) if v > 0],
        "emergency": {"liquid": round(liquid), "avg_expense": round(avg_exp),
                      "months": months, "target": 6},
        "target": round(target), "monthly_save": monthly_save, "saved_month": saved_month,
        "eta_months": eta_months, "eta_date": eta_date,
        "income_sources": db.query(models.Recurring).filter(
            models.Recurring.type == "income", models.Recurring.active.is_(True)).count(),
    }
