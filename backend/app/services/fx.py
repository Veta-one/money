"""
Курсы валют → рубли. USD берём у ЦБ РФ (cbr-xml-daily), кэшируем на день в FxRate.
Крипта-накопления считаем долларовыми (USD). Фолбэк — последний известный курс.
"""
from __future__ import annotations

import logging
from datetime import date

import httpx

from .. import models

log = logging.getLogger("money.fx")
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
_USD_LIKE = {"USD", "USDT", "USDC", "$"}
_FALLBACK_USD = 90.0


def get_usd_rub(db) -> float:
    today = date.today()
    row = db.query(models.FxRate).filter_by(currency="USD", date=today).first()
    if row:
        return row.rate_rub
    try:
        data = httpx.get(CBR_URL, timeout=20).json()
        rate = float(data["Valute"]["USD"]["Value"])
        db.add(models.FxRate(date=today, currency="USD", rate_rub=rate))
        db.commit()
        return rate
    except Exception as e:  # noqa: BLE001
        log.warning("курс ЦБ недоступен: %s", e)
        last = (db.query(models.FxRate).filter_by(currency="USD")
                .order_by(models.FxRate.date.desc()).first())
        return last.rate_rub if last else _FALLBACK_USD


def to_rub(amount: float, currency: str | None, db) -> float:
    cur = (currency or "RUB").upper()
    if cur == "RUB":
        return round(amount, 2)
    if cur in _USD_LIKE:
        return round(amount * get_usd_rub(db), 2)
    return round(amount, 2)  # неизвестная валюта — как есть


def compute_net_worth(db) -> float:
    """Капитал = счета (в рублях) + вам должны − вы должны (по открытым долгам)."""
    total = sum(to_rub(a.balance, a.currency, db)
                for a in db.query(models.Account).filter(models.Account.archived.is_(False)).all())
    for d in db.query(models.Debt).filter(models.Debt.status == "open").all():
        remaining = max((d.amount or 0) - (d.paid or 0), 0)   # в капитал идёт ОСТАТОК долга
        v = to_rub(remaining, d.currency, db)
        total += v if d.direction == "owed_to_me" else -v
    return round(total, 2)
