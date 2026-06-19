"""
Вклады: расчёт доходности — текущая стоимость, начисленные проценты и прогноз
к концу срока.

Модель: тело (`principal`) + опциональные ежемесячные пополнения. Если есть
реальные `DepositTopup` (фактические пополнения по датам) — расчёт идёт по
ним; иначе fallback на `monthly_topup` как «план».

Капитализация:
- с капит: проценты реинвестируются → сложный процент по принципалу и по
  каждому пополнению с момента его внесения.
- без капит: проценты НЕ реинвестируются → линейные. value_now показывает
  всё что заработано (тело + пополнения + накопленные %), даже если % по
  факту выплачиваются на отдельный счёт.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

from .. import models


def _months(a: date | None, b: date | None) -> int:
    if not a or not b or b <= a:
        return 0
    rd = relativedelta(b, a)
    return rd.years * 12 + rd.months


def _grow_compound(principal: float, rate_pct: float, months: int,
                    topup_events: list[tuple[int, float]]) -> float:
    """С капитализацией: principal × (1+r)^n + Σ topup × (1+r)^m
    topup_events: список (m_since_topup, amount)."""
    r = (rate_pct or 0.0) / 100 / 12
    if r <= 0:
        return (principal or 0.0) + sum(a for _, a in topup_events)
    fv = (principal or 0.0) * (1 + r) ** months
    for m, a in topup_events:
        fv += a * ((1 + r) ** m if m > 0 else 1)
    return fv


def _grow_simple(principal: float, rate_pct: float, months: int,
                  topup_events: list[tuple[int, float]]) -> tuple[float, float]:
    """Без капитализации: возвращает (base, interest).
    base = тело + сумма пополнений; interest = накопленные % (линейно)."""
    base = (principal or 0.0) + sum(a for _, a in topup_events)
    r = (rate_pct or 0.0) / 100
    interest = (principal or 0.0) * r * months / 12
    for m, a in topup_events:
        interest += a * r * m / 12
    return base, interest


def _topup_events(d: models.Deposit, today: date, db: Session | None,
                   total_months: int | None = None) -> tuple[list[tuple[int, float]], bool]:
    """Возвращает (события пополнений, есть_ли_реальная_история).
    Каждое событие = (месяцев с момента пополнения до today, сумма).
    Если есть DepositTopup — берём их (реальная история).
    Иначе моделируем monthly_topup как равномерные пополнения с term_start."""
    real: list[models.DepositTopup] = []
    if db is not None:
        real = db.query(models.DepositTopup).filter_by(deposit_id=d.id).all()
    if real:
        events = []
        for t in real:
            if t.date <= today:
                m = _months(t.date, today)
                events.append((m, float(t.amount or 0)))
        return events, True
    # план: monthly_topup × months
    if not d.monthly_topup or d.monthly_topup <= 0:
        return [], False
    events = []
    start = d.term_start or today
    # сколько месяцев на которые натянуть план
    limit = min(_months(start, today), total_months or 10**6)
    for k in range(1, limit + 1):
        # k-е пополнение происходит через k месяцев от старта (== limit-k мес до today)
        events.append((limit - k, float(d.monthly_topup)))
    return events, False


def deposit_view(d: models.Deposit, db: Session | None = None) -> dict:
    today = date.today()
    start = d.term_start or today
    end = d.term_end
    el = _months(start, today)
    tot = _months(start, end) if end else None
    if tot is not None:
        el = min(el, tot)
    events_now, has_real = _topup_events(d, today, db, total_months=el)
    contributed_now = (d.principal or 0) + sum(a for _, a in events_now)
    if d.capitalization:
        value_now = _grow_compound(d.principal, d.rate, el, events_now)
        interest_now = value_now - contributed_now
    else:
        base, interest_now = _grow_simple(d.principal, d.rate, el, events_now)
        value_now = base + interest_now
    # пополнения за текущий календарный месяц (только реальные)
    topup_this_month = 0.0
    topups_list: list[dict] = []
    if has_real and db is not None:
        ms = today.replace(day=1)
        for t in (db.query(models.DepositTopup).filter_by(deposit_id=d.id)
                    .order_by(models.DepositTopup.date.desc()).all()):
            topups_list.append({"id": t.id, "date": t.date.isoformat(),
                                 "amount": round(t.amount or 0, 2)})
            if t.date >= ms:
                topup_this_month += t.amount or 0
    out = {
        "id": d.id, "bank": d.bank or "Вклад", "principal": round(d.principal or 0, 2),
        "rate": d.rate or 0, "monthly_topup": round(d.monthly_topup or 0, 2),
        "capitalization": bool(d.capitalization), "owner": d.owner,
        "term_start": start.isoformat() if start else None,
        "term_end": end.isoformat() if end else None,
        "months_elapsed": el, "value_now": round(value_now, 2),
        "interest_now": round(interest_now, 2),
        "contributed": round(contributed_now, 2),
        "has_real_topups": has_real,
        "topup_this_month": round(topup_this_month, 2),
        "topups": topups_list,
        "source_account_id": getattr(d, "source_account_id", None),
        "currency": (getattr(d, "currency", None) or "RUB").upper(),
        "kind": getattr(d, "kind", None) or "deposit",
    }
    if tot:
        events_end, _ = _topup_events(d, end, db, total_months=tot)
        contrib_end = (d.principal or 0) + sum(a for _, a in events_end)
        if d.capitalization:
            value_end = _grow_compound(d.principal, d.rate, tot, events_end)
            interest_total = value_end - contrib_end
        else:
            base_e, interest_total = _grow_simple(d.principal, d.rate, tot, events_end)
            value_end = base_e + interest_total
        out["months_total"] = tot
        out["value_end"] = round(value_end)
        out["interest_total"] = round(interest_total)
    return out


def deposits_overview(db: Session) -> dict:
    rows = db.query(models.Deposit).order_by(models.Deposit.id.desc()).all()
    items = [deposit_view(d, db) for d in rows]
    return {
        "deposits": items,
        "total_value": round(sum(i["value_now"] for i in items)),
        "total_interest": round(sum(i["interest_now"] for i in items)),
    }


def deposits_total_value(db: Session) -> float:
    """Сумма всех вкладов в рублях (для compute_net_worth).
    Конвертирует валютные вклады в RUB по текущему курсу."""
    from .fx import to_rub
    total = 0.0
    for d in db.query(models.Deposit).all():
        v = deposit_view(d, db)
        val = float(v.get("value_now") or 0.0)
        cur = (getattr(d, "currency", None) or "RUB").upper()
        total += to_rub(val, cur, db)
    return total
