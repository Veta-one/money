"""
Вклады: расчёт доходности — текущая стоимость, начисленные проценты и прогноз к
концу срока (помесячное начисление + ежемесячные пополнения).
"""
from __future__ import annotations

from datetime import date

from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

from .. import models


def _months(a: date | None, b: date | None) -> int:
    if not a or not b or b <= a:
        return 0
    rd = relativedelta(b, a)
    return rd.years * 12 + rd.months


def _grow(principal: float, topup: float, rate_pct: float, months: int) -> float:
    """Стоимость вклада: тело + ежемесячные пополнения с помесячной капитализацией."""
    p, t = principal or 0.0, topup or 0.0
    if months <= 0:
        return p
    r = (rate_pct or 0.0) / 100 / 12
    if r <= 0:
        return p + t * months
    return p * (1 + r) ** months + t * (((1 + r) ** months - 1) / r)


def deposit_view(d: models.Deposit) -> dict:
    today = date.today()
    start = d.term_start or today
    end = d.term_end
    el = _months(start, today)
    tot = _months(start, end) if end else None
    if tot is not None:
        el = min(el, tot)
    value_now = round(_grow(d.principal, d.monthly_topup, d.rate, el))
    contributed_now = round((d.principal or 0) + (d.monthly_topup or 0) * el)
    out = {
        "id": d.id, "bank": d.bank or "Вклад", "principal": round(d.principal or 0),
        "rate": d.rate or 0, "monthly_topup": round(d.monthly_topup or 0),
        "capitalization": bool(d.capitalization), "owner": d.owner,
        "term_start": start.isoformat() if start else None,
        "term_end": end.isoformat() if end else None,
        "months_elapsed": el, "value_now": value_now,
        "interest_now": round(value_now - contributed_now),
    }
    if tot:
        value_end = round(_grow(d.principal, d.monthly_topup, d.rate, tot))
        out["months_total"] = tot
        out["value_end"] = value_end
        out["interest_total"] = round(value_end - ((d.principal or 0) + (d.monthly_topup or 0) * tot))
    return out


def deposits_overview(db: Session) -> dict:
    rows = db.query(models.Deposit).order_by(models.Deposit.id.desc()).all()
    items = [deposit_view(d) for d in rows]
    return {
        "deposits": items,
        "total_value": round(sum(i["value_now"] for i in items)),
        "total_interest": round(sum(i["interest_now"] for i in items)),
    }
