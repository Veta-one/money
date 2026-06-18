"""
Финансовый чекап: сводка «здоровья» + правила-рекомендации + умные инсайты
(аномалии, spending velocity, топ-траты, аудит подписок, pace к цели).
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from .analytics import _sum_by_category, subscriptions
from .capital import capital_overview
from .income import expected_income_monthly
from .planning import avg_monthly_expense
from .settings_store import get_setting


def financial_checkup(db: Session) -> dict:
    income_m = expected_income_monthly(db)
    expense_m = avg_monthly_expense(db)
    savings_m = round(income_m - expense_m)
    rate = round(savings_m / income_m * 100) if income_m > 0 else 0

    cap = capital_overview(db)
    cushion = cap.get("emergency", {}).get("months")
    alloc = cap.get("allocation_currency", [])
    total = sum(a["sum"] for a in alloc) or 1
    rub = next((a["sum"] for a in alloc if a["name"] == "RUB"), 0)
    rub_share = round(rub / total * 100)
    subs = subscriptions(db)["total"]

    recs: list[dict] = []
    if cushion is None:
        recs.append({"l": "info", "t": "Подушка посчитается, когда накопятся снимки капитала и реальные расходы."})
    elif cushion < 3:
        recs.append({"l": "bad", "t": f"Подушка {cushion} мес — мало. Приоритет: довести до 3–6 месяцев расходов."})
    elif cushion < 6:
        recs.append({"l": "warn", "t": f"Подушка {cushion} мес — почти. Добей до 6."})
    else:
        recs.append({"l": "good", "t": f"Подушка {cushion} мес — отлично, форс-мажор закрыт."})

    if income_m > 0:
        if rate < 10:
            recs.append({"l": "bad", "t": f"Норма сбережений {rate}% — низко. Урежь крупнейшую категорию расходов."})
        elif rate < 30:
            recs.append({"l": "warn", "t": f"Норма сбережений {rate}% — ок, но есть куда расти."})
        else:
            recs.append({"l": "good", "t": f"Норма сбережений {rate}% — сильно, так держать."})

    if total > 1 and rub_share < 5:
        recs.append({"l": "warn", "t": "Рублёвой ликвидности почти нет — держи ~1 мес расходов в ₽ на текущие траты."})
    if income_m > 0 and subs > 0.1 * income_m:
        recs.append({"l": "warn", "t": f"Подписки {round(subs)} ₽/мес — заметная доля дохода, пройдись по списку."})

    return {
        "income_m": round(income_m), "expense_m": round(expense_m), "savings_m": savings_m,
        "savings_rate": rate, "cushion_months": cushion,
        "rub_share": rub_share, "usd_share": (100 - rub_share) if total > 1 else None,
        "subscriptions": round(subs), "recommendations": recs[:4],
        "insights": insights(db),
    }


def _fmt(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


def insights(db: Session) -> list[dict]:
    """Живые наблюдения по данным: возвращает список карточек {kind, l, title, text}."""
    now = datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_3_start = month_start - relativedelta(months=3)
    out: list[dict] = []

    # I.1 — аномалии по категориям (текущий месяц vs средние за 3 предыдущих)
    cur = _sum_by_category(db, month_start, month_start + relativedelta(months=1))
    prev = _sum_by_category(db, prev_3_start, month_start)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    today_share = now.day / days_in_month
    for name, c_sum in cur.items():
        if c_sum < 1000:
            continue
        avg = prev.get(name, 0.0) / 3
        if avg <= 0:
            continue
        expected_now = avg * today_share          # сравниваем с долей пройденного месяца
        if c_sum > expected_now * 1.5 and (c_sum - expected_now) > 1500:
            pct = round((c_sum / expected_now - 1) * 100)
            out.append({"kind": "anomaly", "l": "warn",
                        "title": f"«{name}»: расход выше обычного",
                        "text": f"к {now.day}-му числу обычно {_fmt(expected_now)} ₽, сейчас {_fmt(c_sum)} ₽ (+{pct}%)"})

    # I.2 — spending velocity
    spent_now = sum(cur.values())
    spent_avg = sum(prev.values()) / 3
    if spent_avg > 0:
        expected_today = spent_avg * today_share
        if spent_now > expected_today * 1.2 and (spent_now - expected_today) > 3000:
            pct = round((spent_now / expected_today - 1) * 100)
            out.append({"kind": "velocity", "l": "warn",
                        "title": "Тратишь быстрее обычного",
                        "text": f"к {now.day}-му обычно {_fmt(expected_today)} ₽, у тебя {_fmt(spent_now)} ₽ (+{pct}%)"})
        elif spent_now < expected_today * 0.7 and spent_now > 0:
            pct = round((1 - spent_now / expected_today) * 100)
            out.append({"kind": "velocity", "l": "good",
                        "title": "Тратишь медленнее обычного",
                        "text": f"к {now.day}-му обычно {_fmt(expected_today)} ₽, у тебя {_fmt(spent_now)} ₽ (−{pct}%)"})

    # I.3 — топ-3 крупнейших трат текущего месяца
    big = (db.query(models.Transaction).filter(
        models.Transaction.type == "expense",
        models.Transaction.datetime >= month_start)
        .order_by(models.Transaction.base_amount_rub.desc()).limit(3).all())
    big = [t for t in big if (t.base_amount_rub or 0) > 1000]
    if big:
        bits = ", ".join(f"{_fmt(t.base_amount_rub)} ₽ — {(t.merchant or 'операция')[:24]}" for t in big)
        out.append({"kind": "biggest", "l": "info",
                    "title": "Топ крупных трат месяца", "text": bits})

    # I.4 — subscription audit (давно не списывалось / подорожало)
    today_d = now.date()
    for r in db.query(models.Recurring).filter(
            models.Recurring.active.is_(True), models.Recurring.type == "expense").all():
        if r.period != "monthly":
            continue
        last_charge = (db.query(models.Transaction).filter(
            models.Transaction.type == "expense",
            models.Transaction.merchant.ilike(f"%{r.name[:20]}%"))
            .order_by(models.Transaction.datetime.desc()).first())
        if last_charge:
            days = (today_d - last_charge.datetime.date()).days
            if days > 35:
                out.append({"kind": "sub_stale", "l": "warn",
                            "title": f"«{r.name}»: подписка не списывалась {days} дн.",
                            "text": "Проверь, не отменилась ли — или удали из регулярных."})
            elif (last_charge.base_amount_rub or 0) > (r.amount or 0) * 1.2:
                hike = round((last_charge.base_amount_rub / r.amount - 1) * 100)
                out.append({"kind": "sub_hike", "l": "warn",
                            "title": f"«{r.name}»: подорожала",
                            "text": f"было {_fmt(r.amount)} ₽, последнее списание {_fmt(last_charge.base_amount_rub)} ₽ (+{hike}%)"})

    # I.5 — pace к цели капитала
    target = float(get_setting(db, "networth_target") or 0)
    if target > 0:
        from .fx import compute_net_worth
        nw = compute_net_worth(db)
        if nw < target:
            monthly_save = expected_income_monthly(db) - avg_monthly_expense(db)
            if monthly_save > 0:
                eta = round((target - nw) / monthly_save)
                if eta >= 1:
                    out.append({"kind": "goal_pace", "l": "info",
                                "title": "Pace к цели капитала",
                                "text": f"при +{_fmt(monthly_save)} ₽/мес достигнешь {_fmt(target)} ₽ за {eta} мес"})

    # I.6 — bills forecast (повторяющиеся merchants со стабильным интервалом)
    upcoming = _bills_forecast(db, days_ahead=7)
    if upcoming:
        bits = "; ".join(f"{b['merchant']} ≈ {_fmt(b['amount'])} ₽ через {b['days']} дн." for b in upcoming[:3])
        out.append({"kind": "bills", "l": "info",
                    "title": "Скоро ожидаются списания", "text": bits})

    return out


def _bills_forecast(db: Session, days_ahead: int = 7) -> list[dict]:
    """Поиск повторяющихся merchants: если последнее списание было ~28-32 дня назад → скоро ждать."""
    today = datetime.now().date()
    out: list[dict] = []
    for r in db.query(models.Recurring).filter(
            models.Recurring.active.is_(True),
            models.Recurring.type == "expense",
            models.Recurring.period == "monthly").all():
        last = (db.query(models.Transaction).filter(
            models.Transaction.type == "expense",
            models.Transaction.merchant.ilike(f"%{r.name[:20]}%"))
            .order_by(models.Transaction.datetime.desc()).first())
        if not last:
            continue
        next_date = last.datetime.date() + timedelta(days=30)
        days_until = (next_date - today).days
        if 0 <= days_until <= days_ahead:
            out.append({"merchant": r.name, "amount": r.amount, "days": days_until})
    out.sort(key=lambda x: x["days"])
    return out
