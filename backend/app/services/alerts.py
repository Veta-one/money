"""
Алерты и нуджи: уведомления в момент траты (крупная трата, перебор бюджета по
категории) и напоминания продлить регулярные платежи/доходы по сроку.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta

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


async def nudge_job() -> None:
    """Планировщик: ежедневно слать напоминания о продлении."""
    from ..bot import bot
    if not bot:
        return
    db = SessionLocal()
    try:
        msgs = renewal_nudges(db)
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
