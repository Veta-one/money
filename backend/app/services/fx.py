"""
Курсы валют → рубли. USD берём у ЦБ РФ (cbr-xml-daily), кэшируем на день в FxRate.
Крипта-накопления считаем долларовыми (USD). Фолбэк — последний известный курс.
История курса — через `XML_dynamic.asp` ЦБ (один запрос на любой период).
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import httpx

from .. import models

log = logging.getLogger("money.fx")
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
CBR_DYNAMIC_URL = "https://www.cbr.ru/scripts/XML_dynamic.asp"
USD_CODE = "R01235"
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


def backfill_usd_rates(db, days: int = 365) -> int:
    """Тянем у ЦБ архив USD/RUB одним запросом (XML_dynamic) и пишем в FxRate.
    Идемпотентно: пропускает уже сохранённые даты. Если охват уже плотный — не дёргаем."""
    today = date.today()
    start = today - timedelta(days=days)
    existing = {d for (d,) in db.query(models.FxRate.date)
                .filter(models.FxRate.currency == "USD",
                        models.FxRate.date >= start).all()}
    # рабочих дней ~70%; если уже есть половина — считаем покрытие достаточным
    if len(existing) >= int(days * 0.5):
        return 0
    try:
        r = httpx.get(CBR_DYNAMIC_URL, params={
            "date_req1": start.strftime("%d/%m/%Y"),
            "date_req2": today.strftime("%d/%m/%Y"),
            "VAL_NM_RQ": USD_CODE,
        }, timeout=30)
        r.encoding = "windows-1251"
        root = ET.fromstring(r.text)
    except Exception as e:  # noqa: BLE001
        log.warning("CBR XML_dynamic недоступен: %s", e)
        return 0
    added = 0
    for rec in root.findall("Record"):
        d_str = rec.attrib.get("Date") or ""
        try:
            d = datetime.strptime(d_str, "%d.%m.%Y").date()
        except ValueError:
            continue
        if d in existing:
            continue
        val_el = rec.find("Value")
        nom_el = rec.find("Nominal")
        if val_el is None or not val_el.text:
            continue
        try:
            value = float(val_el.text.replace(",", "."))
            nominal = float((nom_el.text or "1").replace(",", ".")) if nom_el is not None else 1.0
        except ValueError:
            continue
        if nominal <= 0:
            continue
        db.add(models.FxRate(date=d, currency="USD", rate_rub=value / nominal))
        added += 1
    if added:
        db.commit()
    return added


def usd_history(db, days: int = 365) -> list[dict]:
    """История курса USD за `days` дней. Подтягивает архив ЦБ, если в БД мало."""
    backfill_usd_rates(db, days)
    start = date.today() - timedelta(days=days)
    rows = (db.query(models.FxRate)
            .filter(models.FxRate.currency == "USD", models.FxRate.date >= start)
            .order_by(models.FxRate.date.asc()).all())
    return [{"date": r.date.isoformat(), "rate": round(r.rate_rub, 4)} for r in rows]


def compute_net_worth(db) -> float:
    """Капитал = счета (в рублях) + вклады (value_now) + вам должны − вы должны."""
    total = sum(to_rub(a.balance, a.currency, db)
                for a in db.query(models.Account).filter(models.Account.archived.is_(False)).all())
    # вклады — отдельная сущность, баланс счёта типа deposit обычно 0
    from .deposits import deposits_total_value
    total += deposits_total_value(db)
    for d in db.query(models.Debt).filter(models.Debt.status == "open").all():
        remaining = max((d.amount or 0) - (d.paid or 0), 0)   # в капитал идёт ОСТАТОК долга
        v = to_rub(remaining, d.currency, db)
        total += v if d.direction == "owed_to_me" else -v
    return round(total, 2)
