"""
Алерты и нуджи: уведомления в момент траты (крупная трата, перебор бюджета по
категории) и напоминания продлить регулярные платежи/доходы по сроку.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..db import SessionLocal
from .planning import category_forecast
from .settings_store import get_setting, set_setting


def _fmt(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


def tx_alerts(db: Session, tx_id: int) -> list[str]:
    """Алерты по только что добавленной трате: крупная сумма + перебор бюджета."""
    tx = db.get(models.Transaction, tx_id)
    if not tx or tx.type != "expense":
        return []
    out: list[str] = []
    amt = abs(tx.base_amount_rub or 0.0)
    big = float(get_setting(db, "alert_big") or 15000)
    if amt >= big:
        out.append(f"⚠️ Крупная трата: <b>{_fmt(amt)} ₽</b>{' — ' + tx.merchant if tx.merchant else ''}")
    if tx.category_id:
        month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        spent = float(db.query(func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
                      .filter(models.Transaction.type == "expense",
                              models.Transaction.category_id == tx.category_id,
                              models.Transaction.datetime >= month_start).scalar() or 0.0)
        cat = db.get(models.Category, tx.category_id)
        manual = db.query(models.Budget).filter(models.Budget.category_id == tx.category_id).first()
        budget = manual.amount if manual else float(category_forecast(db).get(cat.name if cat else "", 0.0))
        if budget > 0 and spent > budget:
            out.append(f"📊 «{cat.name}»: перебор бюджета — потрачено <b>{_fmt(spent)}</b> из {_fmt(budget)} ₽")
    return out


def renewal_nudges(db: Session) -> list[str]:
    """Регулярные с истекающим сроком (≤7 дн), каждый напоминаем один раз."""
    today = date.today()
    rows = (db.query(models.Recurring)
            .filter(models.Recurring.active.is_(True),
                    models.Recurring.end_date.isnot(None),
                    models.Recurring.end_date >= today,
                    models.Recurring.end_date <= today + timedelta(days=7)).all())
    notified = set(json.loads(get_setting(db, "nudged_recurring") or "[]"))
    out, keep = [], set(notified)
    for r in rows:
        key = f"{r.id}:{r.end_date.isoformat()}"
        if key in notified:
            continue
        days = (r.end_date - today).days
        kind = "доход" if r.type == "income" else "платёж"
        out.append(f"🔔 {kind} «{r.name}» заканчивается через {days} дн. "
                   f"({r.end_date.isoformat()}). Продлить?")
        keep.add(key)
    if out:
        set_setting(db, "nudged_recurring", json.dumps(list(keep)))
    return out


def _advance_next_date(d: date, period: str) -> date:
    if period == "yearly":
        return d + relativedelta(years=1)
    if period == "weekly":
        return d + timedelta(days=7)
    return d + relativedelta(months=1)   # monthly по умолчанию


def upcoming_payments(db: Session) -> list[str]:
    """Регулярные платежи у которых next_date наступает завтра или сегодня.
    Каждый напоминаем один раз за цикл (ключ = `id:next_date`).
    Просроченные (next_date < today) — auto-advance до today/завтра."""
    today = date.today()
    notified = set(json.loads(get_setting(db, "nudged_payments") or "[]"))
    keep, out = set(notified), []
    # авто-сдвиг просроченных
    for r in db.query(models.Recurring).filter(
        models.Recurring.active.is_(True),
        models.Recurring.type == "expense",
        models.Recurring.next_date.isnot(None),
        models.Recurring.next_date < today,
    ).all():
        d = r.next_date
        while d < today:
            d = _advance_next_date(d, r.period or "monthly")
        r.next_date = d
    db.commit()
    # завтрашние и сегодняшние
    rows = (db.query(models.Recurring)
            .filter(models.Recurring.active.is_(True),
                    models.Recurring.type == "expense",
                    models.Recurring.next_date.isnot(None),
                    models.Recurring.next_date >= today,
                    models.Recurring.next_date <= today + timedelta(days=1)).all())
    for r in rows:
        key = f"{r.id}:{r.next_date.isoformat()}"
        if key in notified:
            continue
        days = (r.next_date - today).days
        when = "Сегодня" if days == 0 else "Завтра"
        out.append(f"{when} платёж: <b>{r.name}</b> — {_fmt(r.amount)} ₽")
        keep.add(key)
    if out:
        set_setting(db, "nudged_payments", json.dumps(list(keep)))
    return out


def staleness_nudges(db: Session) -> list[str]:
    """Напоминания о застое: давно не грузил выписку / чеки / не обновлял балансы и курсы.
    Каждый kind напоминаем не чаще раза в неделю (ключ-понедельник в Settings)."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    out: list[str] = []

    def fresh(kind: str) -> bool:
        last = get_setting(db, f"nudged_stale_{kind}")
        try:
            return bool(last) and date.fromisoformat(last) >= monday
        except ValueError:
            return False

    def mark(kind: str) -> None:
        set_setting(db, f"nudged_stale_{kind}", today.isoformat())

    # 1) Выписки из банка
    if not fresh("statement"):
        last_st = db.query(func.max(models.Transaction.created_at)).filter(
            models.Transaction.source == "statement").scalar()
        if last_st:
            days = (today - last_st.date()).days
            if days >= 14:
                out.append(f"Давно не загружал выписки — последняя {days} дн. назад. "
                           "Закинь свежий CSV/Excel, чтобы картинка не отставала.")
                mark("statement")

    # 2) Чеки из ФНС
    if not fresh("receipts"):
        last_rcp = db.query(func.max(models.Transaction.created_at)).filter(
            models.Transaction.source == "receipt").scalar()
        if last_rcp:
            days = (today - last_rcp.date()).days
            if days >= 14:
                out.append(f"Чеки из ФНС не подтягиваются {days} дн. — проверь связь "
                           "или подгрузи фото QR.")
                mark("receipts")

    # 3) Балансы (по снимкам капитала)
    if not fresh("balances"):
        last_nw = db.query(func.max(models.NetWorthSnapshot.date)).scalar()
        if last_nw:
            days = (today - last_nw).days
            if days >= 21:
                out.append(f"Балансы счетов не обновлялись {days} дн. — загляни в «Капитал», "
                           "поправь суммы.")
                mark("balances")

    # 4) Курс USD устарел
    if not fresh("fx"):
        last_fx = db.query(func.max(models.FxRate.date)).filter(
            models.FxRate.currency == "USD").scalar()
        if last_fx:
            days = (today - last_fx).days
            if days >= 7:
                out.append(f"Курс USD не обновлялся {days} дн. — пересчёт капитала может "
                           "врать.")
                mark("fx")

    # 5) Учёт «затих»
    if not fresh("activity"):
        last_tx = db.query(func.max(models.Transaction.created_at)).scalar()
        if last_tx:
            days = (today - last_tx.date()).days
            if days >= 5:
                out.append(f"Уже {days} дн. без новых операций. Не забыл вести учёт?")
                mark("activity")

    return out


async def nudge_job() -> None:
    """Планировщик: ежедневно слать напоминания о продлении срока + предстоящих платежах + застое."""
    from ..bot import bot
    if not bot:
        return
    db = SessionLocal()
    try:
        msgs = renewal_nudges(db) + upcoming_payments(db) + staleness_nudges(db)
    finally:
        db.close()
    for msg in msgs:
        try:
            await bot.send_message(settings.owner_tg_id, msg, parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass


async def fns_refresh_job() -> None:
    """Планировщик: держим access-токен ФНС тёплым; если refresh умер — зовём на вход."""
    from .fns import LkdrClient
    try:
        client = LkdrClient()
        if not client.refresh_token:
            return
        await asyncio.to_thread(client.refresh)
    except Exception as e:  # noqa: BLE001
        from ..bot import bot
        if bot:
            try:
                await bot.send_message(
                    settings.owner_tg_id,
                    f"🔑 ФНС: автообновление токена не прошло ({e}). Нужен повторный вход — "
                    "пришли свежие token + refreshToken из браузера.")
            except Exception:  # noqa: BLE001
                pass
