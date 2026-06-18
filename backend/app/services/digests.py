"""
Дайджесты в Telegram: ежедневный / недельный / месячный.
Тексты строятся из БД, отправка — владельцу.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func

from .. import models
from ..db import SessionLocal
from .dashboard import get_dashboard
from .planning import goal_view

log = logging.getLogger("money.digest")


def _expense_sum(db, since, until=None) -> float:
    q = (db.query(func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
         .filter(models.Transaction.type == "expense", models.Transaction.datetime >= since))
    if until:
        q = q.filter(models.Transaction.datetime < until)
    return float(q.scalar() or 0.0)


def build_daily(db) -> str:
    now = datetime.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    spent = _expense_sum(db, day_start)
    d = get_dashboard(db)
    review = db.query(models.Transaction).filter(models.Transaction.status == "needs_review").count()
    lines = [f"🌙 <b>Итог дня</b> · {now.strftime('%d.%m')}",
             f"Сегодня потрачено: <b>{spent:.0f} ₽</b>",
             f"Свободно до конца месяца: <b>{d['safe_to_spend']:.0f} ₽</b> (≈{d['per_day']:.0f}/день)"]
    if review:
        lines.append(f"📝 Ждут разбора: {review}")
    return "\n".join(lines)


def build_weekly(db) -> str:
    now = datetime.now()
    week_start = now - timedelta(days=7)
    cur = _expense_sum(db, week_start)
    prev = _expense_sum(db, now - timedelta(days=14), week_start)
    rows = (db.query(models.Category.name,
                     func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
            .join(models.Transaction, models.Transaction.category_id == models.Category.id)
            .filter(models.Transaction.type == "expense", models.Transaction.datetime >= week_start)
            .group_by(models.Category.name)
            .order_by(func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0).desc())
            .limit(5).all())
    d = get_dashboard(db)
    lines = ["📊 <b>Итоги недели</b>", f"Потрачено: <b>{cur:.0f} ₽</b>"]
    if prev > 0:
        diff = (cur - prev) / prev * 100
        lines.append(f"К прошлой неделе: {'+' if diff >= 0 else ''}{diff:.0f}%")
    if rows:
        lines.append("\n<b>Топ категорий:</b>")
        lines += [f"• {n}: {s:.0f} ₽" for n, s in rows]
    lines.append(f"\n💼 Капитал: {d['net_worth']:.0f} ₽")
    goals = db.query(models.Goal).filter(models.Goal.status == "active").all()
    if goals:
        lines.append("\n🎯 <b>Цели:</b>")
        lines += [f"• {gv['name']}: {gv['pct']}%" + (f" → {gv['eta'][:7]}" if gv['eta'] else "")
                  for gv in (goal_view(g) for g in goals)]
    return "\n".join(lines)


def build_monthly(db) -> str:
    d = get_dashboard(db)
    lines = [f"🗓 <b>Месяц {d['month']}</b>",
             f"Потрачено: <b>{d['spent']:.0f} ₽</b> · Доход: {d['income']:.0f} ₽",
             f"💼 Капитал: {d['net_worth']:.0f} ₽"]
    if d["by_category"]:
        lines.append("\n<b>По категориям:</b>")
        lines += [f"• {c['name']}: {c['sum']:.0f} ₽" for c in d["by_category"][:7]]
    return "\n".join(lines)


_BUILDERS = {"daily": build_daily, "weekly": build_weekly, "monthly": build_monthly}


async def send_digest(kind: str) -> None:
    """Отправить дайджест владельцу (вызывается планировщиком)."""
    from ..bot import bot  # ленивый импорт — избегаем циклической зависимости
    from ..config import settings
    if not bot:
        return
    db = SessionLocal()
    try:
        text = _BUILDERS[kind](db)
    finally:
        db.close()
    try:
        await bot.send_message(settings.owner_tg_id, text, parse_mode="HTML")
    except Exception:  # noqa: BLE001
        log.exception("не удалось отправить дайджест %s", kind)
