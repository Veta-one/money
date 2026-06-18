"""
AI-чат для мини-аппа: пользователь спрашивает по-русски, Gemini отвечает на
данных текущего бюджета, истории трат и капитала.

Принцип: грузим в промпт КОНТЕКСТ (числа за 12 мес, счета, цели, регулярные,
последние операции), просим отвечать ТОЛЬКО по этому контексту. Никаких
действий — только чтение.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from .dashboard import get_dashboard
from .fx import compute_net_worth, to_rub
from .llm import gemini

_MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь",
              "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def _monthly_cashflow(db: Session, months: int = 12) -> list[dict]:
    """[{month:'2026-06', name:'июнь 2026', spent:X, income:Y}, ...] за последние N месяцев."""
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
    spent_b = {k: 0.0 for k in keys}
    inc_b = {k: 0.0 for k in keys}
    rows = (db.query(models.Transaction.datetime, models.Transaction.base_amount_rub,
                     models.Transaction.type)
            .filter(models.Transaction.datetime >= start,
                    models.Transaction.type.in_(("expense", "income"))).all())
    for dt, amt, tp in rows:
        k = (dt.year, dt.month)
        if k in (spent_b if tp == "expense" else inc_b):
            (spent_b if tp == "expense" else inc_b)[k] += amt or 0.0
    return [{
        "month": f"{k[0]:04d}-{k[1]:02d}",
        "name": f"{_MONTHS_RU[k[1]-1]} {k[0]}",
        "spent": round(spent_b[k]),
        "income": round(inc_b[k]),
    } for k in keys]


def _category_history(db: Session, months: int = 12) -> dict[str, list[int]]:
    """{'Продукты': [сумма_за_месяц...], ...} за последние N месяцев (старый→новый)."""
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
    rows = (db.query(models.Transaction.datetime, models.Transaction.base_amount_rub,
                     models.Category.name)
            .join(models.Category, models.Category.id == models.Transaction.category_id)
            .filter(models.Transaction.type == "expense",
                    models.Transaction.datetime >= start).all())
    result: dict[str, list[float]] = {}
    for dt, amt, cname in rows:
        k = (dt.year, dt.month)
        if k not in keys:
            continue
        arr = result.setdefault(cname, [0.0] * len(keys))
        arr[keys.index(k)] += amt or 0.0
    return {k: [round(v) for v in vs] for k, vs in result.items()}


def build_context(db: Session) -> dict:
    """Снимок данных пользователя для промпта AI."""
    today = date.today()
    d = get_dashboard(db)

    accounts = []
    for a in db.query(models.Account).filter(models.Account.archived.is_(False)).all():
        accounts.append({
            "name": a.name, "currency": a.currency, "owner": a.owner,
            "balance": round(a.balance, 2),
            "balance_rub": round(to_rub(a.balance, a.currency, db), 2),
        })

    recurring = db.query(models.Recurring).filter(models.Recurring.active.is_(True)).all()
    rec_income = [{"name": r.name, "amount": round(r.amount),
                   "day": r.day, "period": r.period,
                   "owner": r.owner, "end_date": r.end_date.isoformat() if r.end_date else None}
                  for r in recurring if r.type == "income"]
    rec_expense = [{"name": r.name, "amount": round(r.amount),
                    "period": r.period,
                    "end_date": r.end_date.isoformat() if r.end_date else None}
                   for r in recurring if r.type == "expense"]

    goals = [{"name": g.name, "current": round(g.current_amount),
              "target": round(g.target_amount), "pct": round(g.current_amount / g.target_amount * 100) if g.target_amount else 0,
              "target_date": g.target_date.isoformat() if g.target_date else None}
             for g in db.query(models.Goal).filter(models.Goal.status == "active").all()]

    recent_rows = (db.query(models.Transaction)
                   .order_by(models.Transaction.datetime.desc()).limit(25).all())
    recent = []
    for t in recent_rows:
        cat = db.get(models.Category, t.category_id) if t.category_id else None
        recent.append({
            "dt": t.datetime.strftime("%Y-%m-%d"),
            "merchant": t.merchant or "",
            "category": cat.name if cat else None,
            "amount_rub": round(t.base_amount_rub or 0, 2),
            "type": t.type,
        })

    return {
        "today": today.isoformat(),
        "currency": "RUB",
        "month_current": {
            "name": f"{_MONTHS_RU[date.today().month-1]} {today.year}",
            "spent": round(d["spent"]),
            "income": round(d["income"]),
            "saved": round(d["saved"]),
            "safe_to_spend": round(d["safe_to_spend"]),
            "days_left": d["days_left"],
            "needs_review": d["needs_review"],
            "forecast_total": round(d.get("forecast_total") or 0),
        },
        "month_prev_compare": {
            "spent": round(d["spent_prev"]),
            "income": round(d["income_prev"]),
        },
        "by_category_now": [{"name": c["name"], "sum": round(c["sum"]),
                              "prev_month_sum": round(c.get("prev") or 0)}
                             for c in d["by_category"]],
        "last_12_months_cashflow": _monthly_cashflow(db, 12),
        "category_history_12mo": _category_history(db, 12),
        "accounts": accounts,
        "net_worth_rub": round(compute_net_worth(db)),
        "recurring_income": rec_income,
        "recurring_expense": rec_expense,
        "active_goals": goals,
        "recent_transactions": recent,
    }


_SYSTEM_PROMPT = """Ты — личный финансовый ассистент пользователя. Тебе дан КОНТЕКСТ — снимок его финансов в JSON.

Правила:
- Отвечай по-русски, кратко (1-3 предложения для простых вопросов; до 5 для аналитических).
- Используй ТОЛЬКО числа из контекста. Никогда не выдумывай и не округляй грубо.
- Если в контексте нет нужных данных — честно скажи об этом одной фразой и предложи где смотреть.
- Форматируй суммы как «12 345 ₽». Проценты — целыми.
- На сравнения отвечай числом + знаком (например: «на 12% больше, +3 200 ₽»).
- Никаких советов о покупке/продаже криптовалют, акций или ценных бумаг.
- Никаких советов выходящих за рамки бытовых финансов.
- Не давай советы по налогам/инвестициям с конкретными цифрами — только общие наблюдения по данным."""


async def ask(db: Session, question: str) -> dict:
    """Запрос пользователя → ответ Gemini на текущем контексте."""
    question = (question or "").strip()
    if not question:
        return {"answer": "Сформулируй вопрос — например: «сколько потратил на еду в этом месяце?»"}
    if len(question) > 500:
        return {"answer": "Вопрос слишком длинный, сократи до 500 символов."}

    ctx = build_context(db)
    prompt = (_SYSTEM_PROMPT
              + "\n\n=== КОНТЕКСТ ===\n"
              + json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
              + "\n=== КОНЕЦ КОНТЕКСТА ===\n\n"
              + f"Вопрос: {question}\n\nОтвет:")
    try:
        text = await gemini.text(prompt)
    except Exception as e:  # noqa: BLE001
        return {"answer": f"Не получилось — {e.__class__.__name__}. Попробуй ещё раз через минуту."}
    return {"answer": (text or "").strip()}
