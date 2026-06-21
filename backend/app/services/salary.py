"""
Калькулятор индексации дохода: «какая зарплата должна быть сейчас».

Сравнивает текущую сумму источника дохода с «инфляционным полом» от даты
последнего повышения. Инфляция берётся валютно-корректно: рублёвые источники —
под ₽-инфляцию (fire_inflation), долларовые — под $-инфляцию (fire_inflation_usd),
те же параметры, что в прогнозе капитала. «Справедливо» сверх пола = merit
(по умолчанию 3% — мировая практика CPI + merit за рост ценности).

Рыночную ставку (сколько дадут при смене работы) калькулятор НЕ выдумывает —
её узнают только офферами; в UI про это явная подсказка.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from .. import models
from .settings_store import get_setting

_USD_LIKE = {"USD", "USDT", "USDC", "$"}


def _annual_inflation(db: Session, currency: str | None) -> float:
    """Годовая инфляция в валюте источника (рубль ≈8%, доллар ≈2.5%)."""
    cur = (currency or "RUB").upper()
    if cur in _USD_LIKE:
        return float(get_setting(db, "fire_inflation_usd") or 2.5) / 100.0
    return float(get_setting(db, "fire_inflation") or 8.0) / 100.0


def _merit(db: Session) -> float:
    """Надбавка сверх инфляции за рост ценности сотрудника (CPI + merit)."""
    return float(get_setting(db, "salary_merit_pct") or 3.0) / 100.0


def _months_between(a: date, b: date) -> int:
    return max((b.year - a.year) * 12 + (b.month - a.month) - (1 if b.day < a.day else 0), 0)


def raise_calc(db: Session, rec: models.Recurring) -> dict:
    """Калькулятор индексации для одного источника дохода."""
    today = date.today()
    cur_amount = float(rec.amount or 0.0)
    infl = _annual_inflation(db, rec.currency)
    merit = _merit(db)

    raises = (db.query(models.IncomeRaise)
              .filter(models.IncomeRaise.recurring_id == rec.id)
              .order_by(models.IncomeRaise.date.asc(), models.IncomeRaise.id.asc()).all())
    history, prev = [], None
    for r in raises:
        jump = round((r.amount / prev - 1) * 100, 1) if prev and prev > 0 else None
        history.append({"id": r.id, "date": r.date.isoformat(),
                        "amount": round(r.amount), "jump_pct": jump, "note": r.note or ""})
        prev = r.amount

    last = raises[-1] if raises else None
    last_date = last.date if last else rec.start_date
    has_raise = last is not None

    out: dict = {
        "id": rec.id, "name": rec.name, "currency": (rec.currency or "RUB"),
        "amount": round(cur_amount), "owner": rec.owner,
        "inflation_pct": round(infl * 100, 1), "merit_pct": round(merit * 100, 1),
        "history": history,
        "last_raise": ({"date": last.date.isoformat(), "amount": round(last.amount)}
                       if last else None),
        "months_since": None, "floor": None, "fair": None,
        "behind": None, "behind_pct": None, "real_now": None,
        "status": "never" if not has_raise else "ok",
    }
    if not last_date or cur_amount <= 0:
        return out

    t = max((today - last_date).days, 0) / 365.25
    months = _months_between(last_date, today)
    floor = cur_amount * (1 + infl) ** t                       # чтобы не беднеть
    fair = cur_amount * ((1 + infl) * (1 + merit)) ** t         # инфляция + merit
    behind = floor - cur_amount
    behind_pct = behind / cur_amount * 100 if cur_amount > 0 else 0.0
    real_now = cur_amount / ((1 + infl) ** t) if infl > -1 else cur_amount

    if not has_raise:
        status = "never"
    elif months >= 18 or behind_pct >= 10:
        status = "overdue"
    elif months >= 12 or behind_pct >= 5:
        status = "watch"
    else:
        status = "ok"

    out.update({
        "months_since": months,
        "floor": round(floor), "fair": round(fair),
        "behind": round(behind), "behind_pct": round(behind_pct, 1),
        "real_now": round(real_now), "status": status,
    })
    return out


def income_raises_overview(db: Session) -> dict:
    """Калькулятор индексации по всем активным источникам дохода."""
    srcs = (db.query(models.Recurring)
            .filter(models.Recurring.type == "income",
                    models.Recurring.active.is_(True)).all())
    sources = [raise_calc(db, s) for s in srcs]
    sources.sort(key=lambda s: -(s.get("amount") or 0))
    return {"merit_pct": round(_merit(db) * 100, 1), "sources": sources}


def _sync_current(db: Session, rec: models.Recurring) -> None:
    """Текущая сумма источника = сумма самого свежего по дате повышения."""
    last = (db.query(models.IncomeRaise)
            .filter(models.IncomeRaise.recurring_id == rec.id)
            .order_by(models.IncomeRaise.date.desc(), models.IncomeRaise.id.desc())
            .first())
    if last:
        rec.amount = last.amount


def add_raise(db: Session, rec_id: int, on: date, amount: float, note: str | None = None) -> dict:
    rec = db.get(models.Recurring, rec_id)
    if not rec or rec.type != "income":
        raise ValueError("no source")
    ev = models.IncomeRaise(recurring_id=rec_id, date=on, amount=abs(amount), note=(note or None))
    db.add(ev)
    db.flush()
    _sync_current(db, rec)   # держим Recurring.amount = последняя зарплата
    db.commit()
    return {"id": ev.id}


def delete_raise(db: Session, raise_id: int) -> None:
    ev = db.get(models.IncomeRaise, raise_id)
    if not ev:
        return
    rec = db.get(models.Recurring, ev.recurring_id)
    db.delete(ev)
    db.flush()
    if rec:
        _sync_current(db, rec)
    db.commit()
