"""
Капитал 2.0: линия net worth по снимкам + разложение прироста за период на
сбережения (доход−расход), эффект курса $ и прочее (крипта/правки балансов).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from .fx import compute_net_worth, get_usd_rub
from .trends import networth_series

_USD_LIKE = {"USD", "USDT", "USDC", "$"}


def _sum_tx(db: Session, tx_type: str, since: datetime) -> float:
    return float(db.query(func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
                 .filter(models.Transaction.type == tx_type,
                         models.Transaction.datetime >= since).scalar() or 0.0)


def capital_overview(db: Session) -> dict:
    now = datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    net_now = compute_net_worth(db)
    usd_now = get_usd_rub(db)
    series = networth_series(db, 60)

    # базовый снимок: на/до начала месяца, иначе самый ранний
    base = (db.query(models.NetWorthSnapshot)
            .filter(models.NetWorthSnapshot.date <= month_start.date())
            .order_by(models.NetWorthSnapshot.date.desc()).first())
    from_month_start = base is not None
    if base is None:
        base = (db.query(models.NetWorthSnapshot)
                .order_by(models.NetWorthSnapshot.date.asc()).first())

    delta = delta_days = savings = fx = other = None
    if base is not None and base.date < date.today():
        since = datetime.combine(base.date, datetime.min.time())
        delta = round(net_now - base.total_rub, 2)
        delta_days = (date.today() - base.date).days
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

    return {
        "net_worth": net_now, "usd_rate": round(usd_now, 2),
        "series": series, "delta": delta, "delta_days": delta_days,
        "from_month_start": from_month_start,
        "savings": savings, "fx": fx, "other": other,
        "income_sources": db.query(models.Recurring).filter(
            models.Recurring.type == "income", models.Recurring.active.is_(True)).count(),
    }
