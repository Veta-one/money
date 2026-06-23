"""Тренды для графиков: траты по месяцам + снимки капитала (net worth) + heatmap по дням."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func

from .. import models
from ..db import SessionLocal
from .fx import compute_net_worth

_MONTHS = ["янв", "фев", "мар", "апр", "май", "июн",
           "июл", "авг", "сен", "окт", "ноя", "дек"]


def monthly_spending(db, months: int = 6) -> list[dict]:
    now = datetime.now()
    keys: list[tuple[int, int]] = []
    y, m = now.year, now.month
    for _ in range(months):
        keys.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    keys.reverse()
    start = datetime(keys[0][0], keys[0][1], 1)
    rows = (db.query(models.Transaction.datetime, models.Transaction.base_amount_rub)
            .filter(models.Transaction.type == "expense",
                    models.Transaction.datetime >= start).all())
    buckets = {k: 0.0 for k in keys}
    for dt, amt in rows:
        k = (dt.year, dt.month)
        if k in buckets:
            buckets[k] += amt or 0.0
    return [{"ym": f"{k[0]:04d}-{k[1]:02d}", "year": k[0],
             "label": _MONTHS[k[1] - 1], "spent": round(buckets[k], 2)} for k in keys]


def take_networth_snapshot(db, force: bool = False) -> float:
    """Снимок капитала на сегодня = стабильная точка «начало дня».
    Создаётся один раз (cron в 00:01) и НЕ перезаписывается операциями/правками
    в течение дня — иначе теряется база для delta-карточки.
    force=True переписывает (используется для одноразовых ретро-коррекций)."""
    import json
    from .fx import networth_breakdown
    total = compute_net_worth(db)
    bj = json.dumps(networth_breakdown(db))
    today = date.today()
    row = db.query(models.NetWorthSnapshot).filter_by(date=today).first()
    if row:
        if force:
            row.total_rub = total
            row.breakdown_json = bj
            db.commit()
        elif not row.breakdown_json:        # дозаполнить старый снимок без разбивки
            row.breakdown_json = bj
            db.commit()
        return row.total_rub
    db.add(models.NetWorthSnapshot(date=today, total_rub=total, breakdown_json=bj))
    db.commit()
    return total


def ensure_startup_snapshot(db) -> None:
    """Снимок на старте сервиса — БЕЗ затирания «начала дня» текущим значением.

    Если снимок за сегодня уже есть — не трогаем (он неприкосновенен: рестарт не
    должен двигать базу дня). Если сегодняшнего ещё нет, НЕ берём текущий капитал:
    при рестарте среди дня в «начало дня» попало бы уже изменившееся за день
    значение (напр. подросший курс), и «прирост за сегодня» обнулился бы. Вместо
    этого переносим значение последнего снимка (вчерашнее «закрытие» ≈ сегодняшнее
    «открытие»). Точную точку начала суток поставит крон в 00:01 МСК."""
    import json
    today = date.today()
    if db.query(models.NetWorthSnapshot).filter_by(date=today).first():
        return
    prev = (db.query(models.NetWorthSnapshot)
            .filter(models.NetWorthSnapshot.date < today)
            .order_by(models.NetWorthSnapshot.date.desc()).first())
    if prev is not None:
        db.add(models.NetWorthSnapshot(date=today, total_rub=prev.total_rub,
                                       breakdown_json=prev.breakdown_json))
    else:  # первый запуск, истории нет — берём текущий капитал
        from .fx import networth_breakdown
        db.add(models.NetWorthSnapshot(date=today, total_rub=compute_net_worth(db),
                                       breakdown_json=json.dumps(networth_breakdown(db))))
    db.commit()


def networth_series(db, limit: int = 30) -> list[dict]:
    import json
    rows = (db.query(models.NetWorthSnapshot)
            .order_by(models.NetWorthSnapshot.date.desc()).limit(limit).all())
    out = []
    for r in reversed(rows):
        item = {"date": r.date.isoformat(), "total": round(r.total_rub, 2)}
        if r.breakdown_json:
            try:
                item["breakdown"] = json.loads(r.breakdown_json)
            except Exception:  # noqa: BLE001
                pass
        out.append(item)
    return out


def daily_spending(db, days: int = 365) -> list[dict]:
    """Сумма расходов по календарному дню за последние `days` дней.
    Возвращает массив {date, spent} только для дней с тратами (фронт сам добавит пустые ячейки)."""
    today = date.today()
    start = datetime.combine(today - timedelta(days=days - 1), datetime.min.time())
    rows = (db.query(func.date(models.Transaction.datetime).label("d"),
                     func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0).label("s"))
            .filter(models.Transaction.type == "expense",
                    models.Transaction.datetime >= start)
            .group_by("d").all())
    return [{"date": str(d), "spent": round(float(s), 2)} for d, s in rows if s and float(s) > 0]


def category_sparkline(db, category_id: int, months: int = 6) -> list[float]:
    """Сумма трат категории по месяцам, возвращает массив из `months` чисел (старый→новый)."""
    now = datetime.now()
    keys: list[tuple[int, int]] = []
    y, m = now.year, now.month
    for _ in range(months):
        keys.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    keys.reverse()
    start = datetime(keys[0][0], keys[0][1], 1)
    rows = (db.query(models.Transaction.datetime, models.Transaction.base_amount_rub)
            .filter(models.Transaction.type == "expense",
                    models.Transaction.category_id == category_id,
                    models.Transaction.datetime >= start).all())
    buckets = {k: 0.0 for k in keys}
    for dt, amt in rows:
        k = (dt.year, dt.month)
        if k in buckets:
            buckets[k] += amt or 0.0
    return [round(buckets[k], 2) for k in keys]


def snapshot_job() -> None:
    """Для планировщика — снимок капитала раз в день."""
    db = SessionLocal()
    try:
        take_networth_snapshot(db)
    finally:
        db.close()
