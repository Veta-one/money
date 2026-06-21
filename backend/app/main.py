"""
Точка входа: FastAPI (API мини-аппа) + Telegram webhook в одном процессе.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from aiogram.types import Update
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import bot as botmod
from . import models  # noqa: F401  — регистрируем таблицы в metadata
from .config import settings
from .db import Base, SessionLocal, engine, get_session
from .migrations import run_migrations
from .security import (SESSION_COOKIE, current_user, make_session_token,
                       validate_login_widget)
from .services.ai_chat import ask as ai_ask
from .services.alerts import fns_refresh_job, nudge_job
from .services.analytics import analytics_overview, month_review
from .services.fire import (allocation_target, fire_metrics, net_worth_forecast,
                             rolling_savings_rate, savings_rate_series)
from .services.backup import make_and_send_backup
from .services.budget import budget_overview
from .services.capital import capital_overview
from .services.categorize import learn_rule
from .services.checkup import financial_checkup
from .services.dashboard import get_dashboard, needs_review
from .services.deposits import deposits_overview
from .services.digests import send_digest
from .services.income import income_overview, learn_income_alias
from .services.trends import (daily_spending, monthly_spending, networth_series,
                              snapshot_job, take_networth_snapshot)
from .services.fx import compute_net_worth, get_usd_rub, to_rub, usd_history
from .services.planning import detect_recurring, goal_view, suggest_goals
from .services.settings_store import get_setting, set_setting

scheduler = AsyncIOScheduler(timezone=settings.timezone)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # таблицы + лёгкие миграции (ADD COLUMN / индексы) для живой БД
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    _db0 = SessionLocal()
    try:
        take_networth_snapshot(_db0)   # стартовый снимок капитала
    except Exception:  # noqa: BLE001
        pass
    finally:
        _db0.close()
    if botmod.bot and settings.public_url:
        # Не валим старт, если TLS/DNS ещё не готовы — вебхук поставим позже.
        try:
            await botmod.bot.set_webhook(
                f"{settings.public_url}/webhook",
                secret_token=settings.webhook_secret,
                drop_pending_updates=True,
            )
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger("money").warning("set_webhook отложен: %s", e)
    if botmod.bot:
        scheduler.add_job(send_digest, "cron", args=["daily"], hour=21, minute=0,
                          id="daily", replace_existing=True)
        scheduler.add_job(send_digest, "cron", args=["weekly"], day_of_week="sun", hour=20,
                          minute=0, id="weekly", replace_existing=True)
        scheduler.add_job(send_digest, "cron", args=["monthly"], day=1, hour=10, minute=0,
                          id="monthly", replace_existing=True)
        scheduler.add_job(make_and_send_backup, "cron", hour=3, minute=30,
                          id="backup", replace_existing=True)
        scheduler.add_job(snapshot_job, "cron", hour=0, minute=1,
                          id="snapshot", replace_existing=True)
        scheduler.add_job(nudge_job, "cron", hour=10, minute=30,
                          id="nudge", replace_existing=True)
        scheduler.add_job(fns_refresh_job, "cron", hour=4, minute=0,
                          id="fns_refresh", replace_existing=True)
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    if botmod.bot:
        await botmod.bot.session.close()


app = FastAPI(title="MONEY", lifespan=lifespan)


@app.get("/api/health")
async def health(db: Session = Depends(get_session)):
    from sqlalchemy import text
    try:
        db.execute(text("SELECT 1"))
        return {"ok": True, "env": settings.app_env, "db": "up"}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"db down: {e}")


@app.get("/api/version")
async def data_version(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    """Дешёвый отпечаток состояния данных. Мини-апп сравнивает его при возврате
    фокуса: если изменился (новая операция/счёт/вклад через бота и т.п.) — сбрасывает
    кэш. Меняется при любой значимой мутации без необходимости трогать endpoints."""
    def _q(expr):
        return db.query(expr).scalar()
    parts = [
        _q(func.count(models.Transaction.id)) or 0,
        _q(func.max(models.Transaction.id)) or 0,
        _q(func.max(models.Transaction.created_at)),
        round(_q(func.coalesce(func.sum(models.Account.balance), 0.0)) or 0.0, 2),
        _q(func.count(models.Account.id)) or 0,
        _q(func.count(models.Deposit.id)) or 0,
        round(_q(func.coalesce(func.sum(models.Deposit.principal), 0.0)) or 0.0, 2),
        _q(func.count(models.DepositTopup.id)) or 0,
        _q(func.count(models.Goal.id)) or 0,
        _q(func.count(models.Debt.id)) or 0,
        round(_q(func.coalesce(func.sum(models.Debt.paid), 0.0)) or 0.0, 2),
        _q(func.count(models.Recurring.id)) or 0,
    ]
    return {"version": "|".join(str(p) for p in parts)}


@app.post("/api/auth/telegram")
async def auth_telegram(request: Request, response: Response):
    """Вход на сайте через Telegram Login Widget (браузер, вне Mini App).
    Проверяем подпись, что это владелец, ставим сессионную cookie."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "bad payload")
    u = validate_login_widget(data)
    if int(u["id"]) != settings.owner_tg_id:
        raise HTTPException(403, "not the owner")
    token = make_session_token(u["id"], u.get("first_name") or "")
    secure = settings.public_url.startswith("https")
    response.set_cookie(SESSION_COOKIE, token, max_age=30 * 86400,
                        httponly=True, secure=secure, samesite="lax")
    return {"ok": True, "first_name": u.get("first_name") or ""}


@app.post("/api/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    """Проверка авторизации мини-аппа (только владелец)."""
    return {"user": user}


@app.get("/api/dashboard")
async def dashboard(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    """Сводка для дашборда (только владелец)."""
    return get_dashboard(db)


@app.get("/api/trends")
async def trends(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    return {"months": monthly_spending(db), "networth": networth_series(db)}


@app.get("/api/analytics")
async def analytics(period: str = "month", user: dict = Depends(current_user),
                    db: Session = Depends(get_session)):
    return analytics_overview(db, period)


@app.get("/api/month-review")
async def month_review_ep(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    """Итоги текущего месяца для карточки «Разбор месяца»."""
    return month_review(db)


@app.get("/api/checkup")
async def checkup(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    return financial_checkup(db)


@app.get("/api/transactions")
async def list_transactions(
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    type: str | None = None,
    category_id: int | None = None,
    account_id: int | None = None,
    q: str | None = None,
    merchant: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    review: int | None = None,
    limit: int = 50,
    offset: int = 0,
    user: dict = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Список операций с фильтрами + итог по выборке (Фаза A)."""
    query = db.query(models.Transaction)
    if month:
        try:
            y, m = (int(x) for x in month.split("-"))
            start = datetime(y, m, 1)
            end = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
            query = query.filter(models.Transaction.datetime >= start,
                                 models.Transaction.datetime < end)
        except Exception:  # noqa: BLE001
            pass
    else:
        if date_from:
            try:
                query = query.filter(models.Transaction.datetime >= datetime.fromisoformat(date_from))
            except Exception:  # noqa: BLE001
                pass
        if date_to:
            try:
                query = query.filter(models.Transaction.datetime < datetime.fromisoformat(date_to) + timedelta(days=1))
            except Exception:  # noqa: BLE001
                pass
    if type in ("expense", "income", "transfer", "debt"):
        query = query.filter(models.Transaction.type == type)
    if category_id:
        query = query.filter(models.Transaction.category_id == category_id)
    if account_id:
        query = query.filter(models.Transaction.account_id == account_id)
    if merchant:
        query = query.filter(models.Transaction.merchant == merchant)
    if min_amount is not None:
        query = query.filter(func.abs(models.Transaction.base_amount_rub) >= min_amount)
    if max_amount is not None:
        query = query.filter(func.abs(models.Transaction.base_amount_rub) <= max_amount)

    rows = query.order_by(models.Transaction.datetime.desc()).all()
    if q:
        ql = q.strip().lower()
        rows = [r for r in rows
                if ql in (r.merchant or "").lower() or ql in (r.note or "").lower()]
    if review:
        rows = [r for r in rows if needs_review(r)]

    sum_expense = round(sum(r.base_amount_rub or 0.0 for r in rows if r.type == "expense"), 2)
    sum_income = round(sum(r.base_amount_rub or 0.0 for r in rows if r.type == "income"), 2)
    count = len(rows)
    page = rows[offset:offset + limit]

    cat_map = {c.id: c.name for c in db.query(models.Category).all()}
    out = [{
        "id": t.id, "dt": t.datetime.isoformat(), "amount": round(t.amount, 2),
        "currency": t.currency, "base_rub": round(t.base_amount_rub or 0.0, 2),
        "type": t.type, "merchant": t.merchant or "",
        "category": cat_map.get(t.category_id), "category_id": t.category_id,
        "account_id": t.account_id, "source": t.source, "status": t.status,
        "review": needs_review(t),
    } for t in page]
    return {
        "transactions": out, "count": count,
        "sum_expense": sum_expense, "sum_income": sum_income,
        "offset": offset, "limit": limit, "has_more": offset + limit < count,
    }


@app.get("/api/accounts")
async def accounts(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    rows = (db.query(models.Account).filter(models.Account.archived.is_(False))
            .order_by(models.Account.owner, models.Account.name).all())
    out = [{"id": a.id, "name": a.name, "type": a.type, "currency": a.currency,
            "owner": a.owner, "balance": a.balance,
            "rub": to_rub(a.balance, a.currency, db),
            "interest_rate": float(a.interest_rate or 0.0),
            "interest_compound": bool(a.interest_compound) if a.interest_compound is not None else True,
            "interest_note": a.interest_note or ""} for a in rows]
    return {"accounts": out, "net_worth": compute_net_worth(db), "usd_rate": round(get_usd_rub(db), 2)}


@app.get("/api/capital")
async def capital(period: str = "day", user: dict = Depends(current_user),
                   db: Session = Depends(get_session)):
    return capital_overview(db, period=period)


@app.get("/api/fx/history")
async def fx_history(currency: str = "USD", days: int = 365,
                     user: dict = Depends(current_user),
                     db: Session = Depends(get_session)):
    days = max(7, min(int(days), 365 * 5))
    cur = (currency or "USD").upper()
    if cur != "USD":
        return {"currency": cur, "days": days, "points": [], "latest": None}
    pts = usd_history(db, days)
    return {"currency": "USD", "days": days, "points": pts,
            "latest": pts[-1]["rate"] if pts else None}


@app.get("/api/heatmap")
async def heatmap(days: int = 365,
                  user: dict = Depends(current_user),
                  db: Session = Depends(get_session)):
    days = max(30, min(int(days), 365 * 2))
    pts = daily_spending(db, days)
    mx = max((p["spent"] for p in pts), default=0.0)
    return {"days": days, "points": pts, "max": round(mx, 2)}


@app.get("/api/fire")
async def fire(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    """FIRE-калькулятор: 3 сценария, Years to FI, Coast FI, Runway, целевая аллокация."""
    return {**fire_metrics(db),
            "allocation": allocation_target(db),
            "rolling_savings_rate": rolling_savings_rate(db)}


@app.get("/api/forecast")
async def forecast(years: int | None = None, user: dict = Depends(current_user),
                    db: Session = Depends(get_session)):
    """Прогноз net worth на N лет. Если years не задан — горизонт = до пенсии + 5 (≥20)."""
    return net_worth_forecast(db, years)


@app.get("/api/savings/history")
async def savings_history(months: int = 12, user: dict = Depends(current_user),
                          db: Session = Depends(get_session)):
    """Помесячная норма сбережений (для графика дисциплины во времени)."""
    return savings_rate_series(db, months)


class AskIn(BaseModel):
    question: str


@app.post("/api/ai/ask")
async def ai_ask_endpoint(body: AskIn,
                          user: dict = Depends(current_user),
                          db: Session = Depends(get_session)):
    return await ai_ask(db, body.question)


@app.get("/api/suggest/merchants")
async def suggest_merchants(q: str = "", limit: int = 6,
                            user: dict = Depends(current_user),
                            db: Session = Depends(get_session)):
    """Smart-compose: топ-N merchants по prefix `q`, с категорией и средней суммой."""
    limit = max(1, min(int(limit), 20))
    base = (db.query(models.Transaction.merchant,
                     models.Transaction.category_id,
                     func.count(models.Transaction.id).label("cnt"),
                     func.avg(models.Transaction.amount).label("avg_amt"))
            .filter(models.Transaction.merchant.isnot(None),
                    models.Transaction.merchant != "",
                    models.Transaction.type == "expense"))
    if q:
        base = base.filter(func.lower(models.Transaction.merchant).like(f"%{q.lower()}%"))
    rows = (base.group_by(models.Transaction.merchant, models.Transaction.category_id)
            .order_by(func.count(models.Transaction.id).desc()).limit(limit).all())
    out = []
    seen_merchants: set[str] = set()
    for m, cid, cnt, avg in rows:
        # уникальные merchants — берём наиболее частую категорию
        if m in seen_merchants:
            continue
        seen_merchants.add(m)
        cat = db.get(models.Category, cid) if cid else None
        out.append({
            "merchant": m,
            "category_id": cid, "category": cat.name if cat else None,
            "amount": round(float(avg or 0)), "uses": int(cnt),
        })
    return {"q": q, "suggestions": out}


@app.get("/api/receipts")
async def receipts_list(q: str = "", limit: int = 30, offset: int = 0,
                        user: dict = Depends(current_user),
                        db: Session = Depends(get_session)):
    """Чеки ФНС с item-level поиском. Новые первыми. По q ищем по позициям и магазину."""
    limit = max(1, min(int(limit), 60))
    base_q = (db.query(models.Receipt)
              .join(models.Transaction, models.Transaction.id == models.Receipt.transaction_id))
    if q:
        like = f"%{q.strip().lower()}%"
        # подмножество транзакций с попаданием по имени позиции или магазина
        sub = (db.query(models.TransactionItem.transaction_id)
               .filter(func.lower(models.TransactionItem.name).like(like))).subquery()
        base_q = base_q.filter(or_(
            models.Transaction.id.in_(sub),
            func.lower(models.Receipt.kkt_owner).like(like),
            func.lower(models.Transaction.merchant).like(like),
        ))
    count = base_q.count()
    rows = (base_q.order_by(models.Transaction.datetime.desc())
            .offset(offset).limit(limit).all())
    out = []
    for r in rows:
        t = r.transaction
        items = sorted(t.items, key=lambda i: -(i.sum or 0))[:50]
        out.append({
            "id": r.id, "tx_id": t.id,
            "dt": t.datetime.isoformat(),
            "merchant": r.kkt_owner or t.merchant or "—",
            "place": r.retail_place,
            "total": round(t.base_amount_rub or 0, 2),
            "n_items": len(t.items),
            "items": [{"name": i.name, "qty": i.qty, "price": i.price, "sum": i.sum}
                      for i in items],
        })
    return {"q": q, "count": count, "offset": offset, "limit": limit,
            "has_more": offset + limit < count, "receipts": out}


class TargetIn(BaseModel):
    target: float


@app.post("/api/capital/target")
async def set_nw_target(body: TargetIn, user: dict = Depends(current_user),
                        db: Session = Depends(get_session)):
    set_setting(db, "networth_target", body.target)
    return {"ok": True}


class BalanceIn(BaseModel):
    balance: float


@app.post("/api/accounts/{acc_id}")
async def set_balance(acc_id: int, body: BalanceIn,
                      user: dict = Depends(current_user), db: Session = Depends(get_session)):
    acc = db.get(models.Account, acc_id)
    if not acc:
        raise HTTPException(404, "no account")
    acc.balance = body.balance
    db.commit()
    return {"ok": True}


def _ensure_category(db: Session, name: str, ctype: str) -> models.Category:
    """Найти категорию по имени+типу или создать (для авто-операций сверки)."""
    c = (db.query(models.Category)
         .filter(models.Category.name == name, models.Category.type == ctype,
                 models.Category.archived.is_(False)).first())
    if c:
        return c
    from .services.category_colors import color_for_new_category
    c = models.Category(name=name[:64], type=ctype, parent_id=None,
                        icon="adjust", color=color_for_new_category(db, None))
    db.add(c)
    db.flush()
    return c


class ReconcileIn(BaseModel):
    actual_balance: float


@app.post("/api/accounts/{acc_id}/reconcile")
async def reconcile_account(acc_id: int, body: ReconcileIn,
                            user: dict = Depends(current_user), db: Session = Depends(get_session)):
    """Сверка: пользователь вводит РЕАЛЬНЫЙ баланс счёта из банка. Считаем
    расхождение с учётом, создаём корректирующую операцию на разницу
    (категория «Корректировка») и выставляем фактический баланс. Так net worth
    всегда честный, а сумма корректировок показывает «сколько утекает мимо учёта»."""
    acc = db.get(models.Account, acc_id)
    if not acc:
        raise HTTPException(404, "no account")
    current = round(acc.balance or 0.0, 2)
    actual = round(body.actual_balance, 2)
    diff = round(actual - current, 2)
    tx_id = None
    if abs(diff) >= 0.01:
        cur = (acc.currency or "RUB").upper()
        ttype = "income" if diff > 0 else "expense"
        amt = abs(diff)
        base = to_rub(amt, cur, db)
        cat = _ensure_category(db, "Корректировка", ttype)
        t = models.Transaction(
            type=ttype, amount=amt, currency=cur, base_amount_rub=base,
            fx_rate=(base / amt if amt else 1.0),
            category_id=cat.id, account_id=acc.id,
            merchant="Сверка баланса",
            note=f"Сверка: учёт {current} → факт {actual} ({'+' if diff > 0 else ''}{diff} {cur})",
            datetime=datetime.now(), source="reconcile", status="confirmed",
        )
        db.add(t)
        db.flush()
        tx_id = t.id
    acc.balance = actual
    db.commit()
    take_networth_snapshot(db, force=True)
    return {"diff": diff, "old": current, "new": actual, "currency": acc.currency, "tx_id": tx_id}


class AccIn(BaseModel):
    name: str
    type: str = "card"
    currency: str = "RUB"
    owner: str = "me"
    balance: float = 0.0
    interest_rate: float = 0.0
    interest_compound: bool = True
    interest_note: str | None = None


class AccEdit(BaseModel):
    name: str | None = None
    type: str | None = None
    currency: str | None = None
    owner: str | None = None
    balance: float | None = None
    interest_rate: float | None = None
    interest_compound: bool | None = None
    interest_note: str | None = None


@app.post("/api/accounts")
async def create_account(body: AccIn, user: dict = Depends(current_user),
                         db: Session = Depends(get_session)):
    a = models.Account(
        name=(body.name[:128] or "Счёт"),
        type=body.type if body.type in ("card", "cash", "deposit", "crypto", "external") else "card",
        currency=(body.currency or "RUB").upper(),
        owner=body.owner if body.owner in ("me", "wife") else "me",
        balance=body.balance or 0.0,
        interest_rate=max(0.0, float(body.interest_rate or 0.0)),
        interest_compound=bool(body.interest_compound) if body.interest_compound is not None else True,
        interest_note=(body.interest_note or "")[:128] or None)
    db.add(a)
    db.commit()
    return {"id": a.id}


@app.post("/api/accounts/{acc_id}/edit")
async def edit_account(acc_id: int, body: AccEdit, user: dict = Depends(current_user),
                       db: Session = Depends(get_session)):
    a = db.get(models.Account, acc_id)
    if not a:
        raise HTTPException(404, "no account")
    if body.name is not None:
        a.name = body.name[:128]
    if body.type is not None and body.type in ("card", "cash", "deposit", "crypto", "external"):
        a.type = body.type
    if body.currency is not None:
        a.currency = body.currency.upper()
    if body.owner in ("me", "wife"):
        a.owner = body.owner
    if body.balance is not None:
        a.balance = body.balance
    if body.interest_rate is not None:
        a.interest_rate = max(0.0, float(body.interest_rate))
    if body.interest_compound is not None:
        a.interest_compound = bool(body.interest_compound)
    if body.interest_note is not None:
        a.interest_note = (body.interest_note or "")[:128] or None
    db.commit()
    return {"ok": True}


@app.delete("/api/accounts/{acc_id}")
async def delete_account(acc_id: int, user: dict = Depends(current_user),
                         db: Session = Depends(get_session)):
    a = db.get(models.Account, acc_id)
    if a:
        a.archived = True   # архивируем, чтобы не осиротить операции
        db.commit()
    return {"ok": True}


class SettingsIn(BaseModel):
    expected_monthly_income: float


@app.get("/api/settings")
async def read_settings(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    return {"expected_monthly_income": float(get_setting(db, "expected_monthly_income") or 0)}


@app.post("/api/settings")
async def write_settings(body: SettingsIn,
                         user: dict = Depends(current_user), db: Session = Depends(get_session)):
    set_setting(db, "expected_monthly_income", body.expected_monthly_income)
    return {"ok": True}


# ---------- цели ----------

class GoalIn(BaseModel):
    name: str
    target_amount: float
    monthly_plan: float = 0
    current_amount: float = 0
    target_date: str | None = None
    account_id: int | None = None


class GoalPatch(BaseModel):
    current_amount: float | None = None
    monthly_plan: float | None = None
    target_amount: float | None = None
    target_date: str | None = None
    status: str | None = None
    account_id: int | None = None


@app.get("/api/goals")
async def list_goals(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    goals = db.query(models.Goal).filter(models.Goal.status != "done").all()
    return {"goals": [goal_view(g, db) for g in goals], "suggest": suggest_goals(db)}


@app.post("/api/goals")
async def create_goal(body: GoalIn, user: dict = Depends(current_user), db: Session = Depends(get_session)):
    g = models.Goal(
        name=body.name, target_amount=body.target_amount, monthly_plan=body.monthly_plan,
        current_amount=body.current_amount, account_id=body.account_id,
        target_date=date.fromisoformat(body.target_date) if body.target_date else None,
        status="active")
    db.add(g)
    db.commit()
    return goal_view(g, db)


@app.post("/api/goals/{goal_id}")
async def patch_goal(goal_id: int, body: GoalPatch,
                     user: dict = Depends(current_user), db: Session = Depends(get_session)):
    g = db.get(models.Goal, goal_id)
    if not g:
        raise HTTPException(404, "no goal")
    for field in ("current_amount", "monthly_plan", "target_amount", "status"):
        v = getattr(body, field)
        if v is not None:
            setattr(g, field, v)
    if body.account_id is not None:
        g.account_id = body.account_id or None
    if body.target_date is not None:
        g.target_date = date.fromisoformat(body.target_date) if body.target_date else None
    db.commit()
    return goal_view(g, db)


@app.delete("/api/goals/{goal_id}")
async def delete_goal(goal_id: int, user: dict = Depends(current_user), db: Session = Depends(get_session)):
    g = db.get(models.Goal, goal_id)
    if g:
        db.delete(g)
        db.commit()
    return {"ok": True}


# ---------- регулярные платежи ----------

class RecurringIn(BaseModel):
    name: str
    amount: float
    type: str = "expense"
    period: str = "monthly"
    day: int | None = None
    next_date: str | None = None


def _parse_date(s: str | None):
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/recurring")
async def list_recurring(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    rows = (db.query(models.Recurring)
            .filter(models.Recurring.active.is_(True),
                    models.Recurring.type == "expense").all())
    return {"recurring": [{"id": r.id, "name": r.name, "amount": r.amount, "type": r.type,
                           "period": r.period, "day": r.day,
                           "next_date": r.next_date.isoformat() if r.next_date else None}
                          for r in rows],
            "candidates": detect_recurring(db)}


@app.post("/api/recurring")
async def create_recurring(body: RecurringIn, user: dict = Depends(current_user),
                           db: Session = Depends(get_session)):
    r = models.Recurring(name=body.name, amount=body.amount,
                         type=body.type if body.type in ("expense", "income") else "expense",
                         period=body.period, day=body.day, active=True,
                         next_date=_parse_date(body.next_date))
    db.add(r)
    db.commit()
    return {"id": r.id}


@app.delete("/api/recurring/{rec_id}")
async def delete_recurring(rec_id: int, user: dict = Depends(current_user),
                           db: Session = Depends(get_session)):
    r = db.get(models.Recurring, rec_id)
    if r:
        db.delete(r)
        db.commit()
    return {"ok": True}


class DismissIn(BaseModel):
    name: str


@app.post("/api/recurring/dismiss")
async def dismiss_recurring(body: DismissIn, user: dict = Depends(current_user),
                            db: Session = Depends(get_session)):
    lst = json.loads(get_setting(db, "dismissed_recurring") or "[]")
    if body.name not in lst:
        lst.append(body.name)
    set_setting(db, "dismissed_recurring", json.dumps(lst, ensure_ascii=False))
    return {"ok": True}


class RecPatch(BaseModel):
    amount: float | None = None
    name: str | None = None
    next_date: str | None = None
    clear_next_date: bool | None = None


@app.post("/api/recurring/{rec_id}")
async def patch_recurring(rec_id: int, body: RecPatch, user: dict = Depends(current_user),
                          db: Session = Depends(get_session)):
    r = db.get(models.Recurring, rec_id)
    if not r:
        raise HTTPException(404, "no recurring")
    if body.amount is not None:
        r.amount = body.amount
    if body.name is not None:
        r.name = body.name[:128]
    if body.clear_next_date:
        r.next_date = None
    elif body.next_date is not None:
        r.next_date = _parse_date(body.next_date)
    db.commit()
    return {"ok": True}


# ---------- доходы по источникам ----------

class IncomeIn(BaseModel):
    name: str
    amount: float
    currency: str = "RUB"
    period: str = "monthly"
    owner: str = "me"
    day: int | None = None
    start_date: str | None = None
    end_date: str | None = None


class IncomePatch(BaseModel):
    name: str | None = None
    amount: float | None = None
    currency: str | None = None
    period: str | None = None
    owner: str | None = None
    end_date: str | None = None
    active: bool | None = None


@app.get("/api/income")
async def income(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    return income_overview(db)


@app.post("/api/income")
async def create_income(body: IncomeIn, user: dict = Depends(current_user),
                        db: Session = Depends(get_session)):
    r = models.Recurring(
        name=body.name[:128], amount=abs(body.amount), currency=(body.currency or "RUB"),
        period=body.period if body.period in ("monthly", "yearly", "weekly") else "monthly",
        owner=body.owner if body.owner in ("me", "wife") else "me",
        day=body.day, type="income", active=True,
        start_date=date.fromisoformat(body.start_date) if body.start_date else None,
        end_date=date.fromisoformat(body.end_date) if body.end_date else None,
    )
    db.add(r)
    db.commit()
    return {"id": r.id}


@app.post("/api/income/{rec_id}")
async def patch_income(rec_id: int, body: IncomePatch, user: dict = Depends(current_user),
                       db: Session = Depends(get_session)):
    r = db.get(models.Recurring, rec_id)
    if not r or r.type != "income":
        raise HTTPException(404, "no source")
    for field in ("name", "amount", "currency", "period", "owner", "active"):
        v = getattr(body, field)
        if v is not None:
            setattr(r, field, v)
    if body.end_date is not None:
        r.end_date = date.fromisoformat(body.end_date) if body.end_date else None
    db.commit()
    return {"ok": True}


@app.delete("/api/income/{rec_id}")
async def delete_income(rec_id: int, user: dict = Depends(current_user),
                        db: Session = Depends(get_session)):
    r = db.get(models.Recurring, rec_id)
    if r and r.type == "income":
        db.delete(r)
        db.commit()
    return {"ok": True}


# ---------- калькулятор индексации дохода ----------

class RaiseIn(BaseModel):
    date: str
    amount: float
    note: str | None = None


@app.get("/api/income/raises")
async def income_raises(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    from .services.salary import income_raises_overview
    return income_raises_overview(db)


@app.post("/api/income/{rec_id}/raise")
async def income_add_raise(rec_id: int, body: RaiseIn, user: dict = Depends(current_user),
                           db: Session = Depends(get_session)):
    from .services.salary import add_raise
    try:
        on = date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(400, "bad date")
    try:
        return add_raise(db, rec_id, on, body.amount, body.note)
    except ValueError:
        raise HTTPException(404, "no source")


@app.delete("/api/income/raise/{raise_id}")
async def income_del_raise(raise_id: int, user: dict = Depends(current_user),
                           db: Session = Depends(get_session)):
    from .services.salary import delete_raise
    delete_raise(db, raise_id)
    return {"ok": True}


# ---------- бюджет по категориям ----------

class BudgetIn(BaseModel):
    category_id: int
    amount: float


@app.get("/api/budgets")
async def budgets(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    return budget_overview(db)


@app.post("/api/budgets")
async def set_budget(body: BudgetIn, user: dict = Depends(current_user),
                     db: Session = Depends(get_session)):
    b = db.query(models.Budget).filter(models.Budget.category_id == body.category_id).first()
    if body.amount and body.amount > 0:
        if b:
            b.amount = body.amount
        else:
            db.add(models.Budget(category_id=body.category_id, amount=body.amount))
    elif b:
        db.delete(b)  # 0 → вернуться к авто-прогнозу
    db.commit()
    return {"ok": True}


# ---------- вклады ----------

class DepIn(BaseModel):
    bank: str
    principal: float = 0.0
    rate: float = 0.0
    monthly_topup: float = 0.0
    capitalization: bool = True
    owner: str = "me"
    term_start: str | None = None
    term_end: str | None = None
    # откуда взяли (списываем balance) — обязательно если principal>0
    source_account_id: int | None = None
    currency: str | None = None        # автоподхват из source если не задано
    kind: str | None = None            # 'deposit'|'staking'|'other' — для иконки


class DepPatch(BaseModel):
    bank: str | None = None
    principal: float | None = None
    rate: float | None = None
    monthly_topup: float | None = None
    capitalization: bool | None = None
    owner: str | None = None
    term_start: str | None = None
    term_end: str | None = None
    currency: str | None = None
    kind: str | None = None


def _guess_deposit_kind(name: str, source_cur: str) -> str:
    """Эвристика для иконки: крипта если USDT/USDC/USD-источник и/или название
    содержит крипто-маркер; иначе обычный вклад."""
    n = (name or "").lower()
    cur = (source_cur or "").upper()
    crypto_markers = ("binance", "bybit", "okx", "kraken", "telegram", "usdt", "usdc",
                       "стейкинг", "staking", "крипт", "ton ", "eth", "wallet")
    if cur in ("USDT", "USDC") or any(m in n for m in crypto_markers):
        return "staking"
    return "deposit"


@app.get("/api/deposits")
async def deposits(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    return deposits_overview(db)


@app.post("/api/deposits")
async def create_deposit(body: DepIn, user: dict = Depends(current_user),
                         db: Session = Depends(get_session)):
    principal = max(0.0, float(body.principal or 0.0))
    # источник: списываем principal с его balance, валюту берём из него если не задано явно
    src = db.get(models.Account, body.source_account_id) if body.source_account_id else None
    if principal > 0 and not src:
        raise HTTPException(400, "Укажите счёт-источник: с него спишется сумма")
    currency = (body.currency or (src.currency if src else "RUB") or "RUB").upper()
    kind = (body.kind or _guess_deposit_kind(body.bank, src.currency if src else "")) or "deposit"
    d = models.Deposit(
        bank=body.bank[:128], principal=principal, rate=body.rate or 0.0,
        monthly_topup=body.monthly_topup or 0.0, capitalization=bool(body.capitalization),
        owner=body.owner if body.owner in ("me", "wife") else "me",
        term_start=date.fromisoformat(body.term_start) if body.term_start else date.today(),
        term_end=date.fromisoformat(body.term_end) if body.term_end else None,
        source_account_id=src.id if src else None,
        currency=currency, kind=kind if kind in ("deposit", "staking", "other") else "deposit",
    )
    if src and principal > 0:
        src.balance = round((src.balance or 0) - principal, 2)
    db.add(d)
    db.commit()
    return {"id": d.id}


@app.post("/api/deposits/{dep_id}")
async def patch_deposit(dep_id: int, body: DepPatch, user: dict = Depends(current_user),
                        db: Session = Depends(get_session)):
    d = db.get(models.Deposit, dep_id)
    if not d:
        raise HTTPException(404, "no deposit")
    for f in ("bank", "principal", "rate", "monthly_topup", "capitalization", "owner", "kind"):
        v = getattr(body, f)
        if v is not None:
            setattr(d, f, v)
    if body.currency is not None:
        d.currency = (body.currency or "RUB").upper()
    if body.term_start is not None:
        d.term_start = date.fromisoformat(body.term_start) if body.term_start else None
    if body.term_end is not None:
        d.term_end = date.fromisoformat(body.term_end) if body.term_end else None
    db.commit()
    return {"ok": True}


@app.delete("/api/deposits/{dep_id}")
async def delete_deposit(dep_id: int, user: dict = Depends(current_user),
                         db: Session = Depends(get_session)):
    d = db.get(models.Deposit, dep_id)
    if d:
        # вместе с вкладом сносим его пополнения. Balance счёта-источника НЕ возвращаем —
        # пользователь сам отрегулирует если нужно (деньги физически могли уйти куда-то ещё).
        db.query(models.DepositTopup).filter_by(deposit_id=dep_id).delete()
        db.delete(d)
        db.commit()
    return {"ok": True}


class TopupIn(BaseModel):
    amount: float
    date: str | None = None
    note: str | None = None
    source_account_id: int | None = None


@app.post("/api/deposits/{dep_id}/topup")
async def add_topup(dep_id: int, body: TopupIn, user: dict = Depends(current_user),
                    db: Session = Depends(get_session)):
    dep = db.get(models.Deposit, dep_id)
    if not dep:
        raise HTTPException(404, "no deposit")
    amount = abs(float(body.amount))
    # источник: если не задан явно — берём source_account вклада
    src_id = body.source_account_id or dep.source_account_id
    src = db.get(models.Account, src_id) if src_id else None
    if amount > 0 and not src:
        raise HTTPException(400, "Укажите счёт-источник для пополнения")
    t = models.DepositTopup(
        deposit_id=dep_id, amount=amount,
        date=date.fromisoformat(body.date) if body.date else date.today(),
        note=(body.note or "")[:256] or None,
        source_account_id=src.id if src else None)
    if src and amount > 0:
        src.balance = round((src.balance or 0) - amount, 2)
    db.add(t)
    db.commit()
    return {"id": t.id}


@app.delete("/api/deposits/topup/{topup_id}")
async def delete_topup(topup_id: int, user: dict = Depends(current_user),
                       db: Session = Depends(get_session)):
    t = db.get(models.DepositTopup, topup_id)
    if t:
        db.delete(t)
        db.commit()
    return {"ok": True}


# ---------- долги ----------

class DebtIn(BaseModel):
    counterparty: str
    direction: str            # i_owe | owed_to_me
    amount: float
    currency: str = "RUB"


@app.get("/api/debts")
async def list_debts(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    rows = (db.query(models.Debt).filter(models.Debt.status == "open")
            .order_by(models.Debt.id.desc()).all())

    def _rem(d):
        return max((d.amount or 0) - (d.paid or 0), 0)

    owed = sum(to_rub(_rem(d), d.currency, db) for d in rows if d.direction == "owed_to_me")
    iowe = sum(to_rub(_rem(d), d.currency, db) for d in rows if d.direction == "i_owe")
    return {"debts": [{"id": d.id, "counterparty": d.counterparty, "direction": d.direction,
                       "amount": round(d.amount or 0), "paid": round(d.paid or 0),
                       "remaining": round(_rem(d)), "currency": d.currency} for d in rows],
            "owed_to_me": round(owed, 2), "i_owe": round(iowe, 2)}


@app.post("/api/debts")
async def create_debt(body: DebtIn, user: dict = Depends(current_user), db: Session = Depends(get_session)):
    d = models.Debt(counterparty=body.counterparty[:128],
                    direction=body.direction if body.direction in ("i_owe", "owed_to_me") else "owed_to_me",
                    amount=abs(body.amount), currency=(body.currency or "RUB"),
                    date=date.today(), status="open")
    db.add(d)
    db.commit()
    return {"id": d.id}


@app.post("/api/debts/{debt_id}/close")
async def close_debt(debt_id: int, user: dict = Depends(current_user), db: Session = Depends(get_session)):
    d = db.get(models.Debt, debt_id)
    if d:
        d.status = "closed"
        db.commit()
    return {"ok": True}


class PayIn(BaseModel):
    amount: float


@app.post("/api/debts/{debt_id}/pay")
async def pay_debt(debt_id: int, body: PayIn, user: dict = Depends(current_user),
                   db: Session = Depends(get_session)):
    """Записать частичное погашение долга (или возврат вам)."""
    d = db.get(models.Debt, debt_id)
    if not d:
        raise HTTPException(404, "no debt")
    d.paid = (d.paid or 0) + abs(body.amount)
    if d.paid >= (d.amount or 0):       # погашено полностью → закрываем
        d.paid = d.amount or 0
        d.status = "closed"
    db.commit()
    return {"ok": True}


@app.delete("/api/debts/{debt_id}")
async def delete_debt(debt_id: int, user: dict = Depends(current_user), db: Session = Depends(get_session)):
    d = db.get(models.Debt, debt_id)
    if d:
        db.delete(d)
        db.commit()
    return {"ok": True}


@app.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
):
    if not botmod.bot:
        raise HTTPException(503, "bot not configured")
    if x_telegram_bot_api_secret_token != settings.webhook_secret:
        raise HTTPException(403, "bad webhook secret")
    update = Update.model_validate(await request.json(), context={"bot": botmod.bot})
    await botmod.dp.feed_update(botmod.bot, update)
    return {"ok": True}


# ---------- детали операции и правка категорий ----------

def _recompute_tx_category(tx) -> None:
    sums: dict[int, float] = {}
    for it in tx.items:
        if it.category_id:
            sums[it.category_id] = sums.get(it.category_id, 0.0) + (it.sum or 0.0)
    if sums:
        tx.category_id = max(sums, key=sums.get)


@app.get("/api/categories")
async def list_categories(type: str = "expense", user: dict = Depends(current_user),
                          db: Session = Depends(get_session)):
    ctype = type if type in ("expense", "income", "transfer") else "expense"
    counts = dict(db.query(models.Transaction.category_id, func.count(models.Transaction.id))
                  .filter(models.Transaction.category_id.isnot(None))
                  .group_by(models.Transaction.category_id).all())
    cats = (db.query(models.Category)
            .filter(models.Category.type == ctype, models.Category.archived.is_(False)).all())
    childsum: dict[int, int] = {}
    for c in cats:
        if c.parent_id:
            childsum[c.parent_id] = childsum.get(c.parent_id, 0) + counts.get(c.id, 0)
    cats.sort(key=lambda c: (-(counts.get(c.id, 0) + childsum.get(c.id, 0)), c.name))
    return {"categories": [{"id": c.id, "name": c.name, "parent_id": c.parent_id,
                            "icon": c.icon, "color": c.color,
                            "tx": counts.get(c.id, 0)} for c in cats]}


class CategoryIn(BaseModel):
    name: str
    type: str = "expense"
    parent_id: int | None = None
    icon: str | None = None
    color: str | None = None


class CategoryPatch(BaseModel):
    name: str | None = None
    parent_id: int | None = None
    icon: str | None = None
    color: str | None = None


@app.post("/api/categories")
async def create_category(body: CategoryIn, user: dict = Depends(current_user),
                          db: Session = Depends(get_session)):
    from .services.category_colors import color_for_new_category
    ctype = body.type if body.type in ("expense", "income", "transfer") else "expense"
    color = body.color or color_for_new_category(db, body.parent_id)
    c = models.Category(name=body.name[:64], type=ctype, parent_id=body.parent_id,
                        icon=body.icon, color=color)
    db.add(c)
    db.commit()
    return {"id": c.id}


@app.post("/api/categories/{cat_id}")
async def patch_category(cat_id: int, body: CategoryPatch, user: dict = Depends(current_user),
                         db: Session = Depends(get_session)):
    c = db.get(models.Category, cat_id)
    if not c:
        raise HTTPException(404, "no category")
    if body.name is not None:
        c.name = body.name[:64]
    if body.parent_id is not None:
        c.parent_id = body.parent_id or None
    if body.icon is not None:
        c.icon = body.icon or None
    if body.color is not None:
        c.color = body.color or None
    db.commit()
    return {"ok": True}


@app.delete("/api/categories/{cat_id}")
async def delete_category(cat_id: int, user: dict = Depends(current_user),
                          db: Session = Depends(get_session)):
    c = db.get(models.Category, cat_id)
    if not c:
        return {"ok": True}
    used = (db.query(models.Transaction).filter(models.Transaction.category_id == cat_id).count()
            + db.query(models.TransactionItem).filter(models.TransactionItem.category_id == cat_id).count())
    if used:
        c.archived = True   # есть привязки — архивируем, чтобы не осиротить операции
    else:
        db.delete(c)
    db.commit()
    return {"ok": True, "archived": bool(used)}


@app.get("/api/category/{cat_id}/history")
async def category_history(cat_id: int, months: int = 12,
                           user: dict = Depends(current_user), db: Session = Depends(get_session)):
    """Помесячная динамика категории (с подкатегориями) + тренд: средняя за
    завершённые месяцы, текущий месяц, отклонение. Для drill «эта категория во времени»."""
    cat = db.get(models.Category, cat_id)
    if not cat:
        raise HTTPException(404, "no category")
    months = max(3, min(int(months), 24))
    child_ids = [r[0] for r in db.query(models.Category.id)
                 .filter(models.Category.parent_id == cat_id).all()]
    ids = [cat_id] + child_ids
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
            .filter(models.Transaction.type == cat.type,
                    models.Transaction.category_id.in_(ids),
                    models.Transaction.datetime >= start).all())
    buckets = {k: 0.0 for k in keys}
    for dt, amt in rows:
        k = (dt.year, dt.month)
        if k in buckets:
            buckets[k] += amt or 0.0
    points = [{"ym": f"{k[0]}-{k[1]:02d}", "value": round(buckets[k], 2)} for k in keys]
    vals = [p["value"] for p in points]
    completed = vals[:-1]                       # без текущего неполного месяца
    nonzero = [v for v in completed if v > 0]
    avg = round(sum(nonzero) / len(nonzero), 2) if nonzero else 0.0
    current = vals[-1] if vals else 0.0
    peak = max(vals) if vals else 0.0
    # тренд: наклон по завершённым месяцам (последняя половина vs первая)
    trend = 0
    if len(completed) >= 4:
        half = len(completed) // 2
        first = sum(completed[:half]) / max(1, half)
        last = sum(completed[half:]) / max(1, len(completed) - half)
        if first > 0:
            ch = (last - first) / first
            trend = 1 if ch > 0.12 else (-1 if ch < -0.12 else 0)
    return {"id": cat.id, "name": cat.name, "color": cat.color, "type": cat.type,
            "has_children": bool(child_ids), "points": points,
            "avg": avg, "current": current, "peak": round(peak, 2),
            "total": round(sum(vals), 2), "months": months, "trend": trend}


class KVIn(BaseModel):
    value: str


@app.get("/api/kv/{key}")
async def kv_get(key: str, user: dict = Depends(current_user), db: Session = Depends(get_session)):
    return {"key": key, "value": get_setting(db, key)}


@app.post("/api/kv/{key}")
async def kv_set(key: str, body: KVIn, user: dict = Depends(current_user),
                 db: Session = Depends(get_session)):
    set_setting(db, key, body.value)
    return {"ok": True}


@app.get("/api/tx/{tx_id}")
async def tx_detail(tx_id: int, user: dict = Depends(current_user), db: Session = Depends(get_session)):
    t = db.get(models.Transaction, tx_id)
    if not t:
        raise HTTPException(404, "no tx")

    def cname(cid):
        c = db.get(models.Category, cid) if cid else None
        return c.name if c else None

    items = [{"id": it.id, "name": it.name, "sum": it.sum, "qty": it.qty,
              "category_id": it.category_id, "category": cname(it.category_id)} for it in t.items]
    src_list = None
    if t.type == "income":
        src_list = [{"id": r.id, "name": r.name} for r in db.query(models.Recurring)
                    .filter(models.Recurring.type == "income", models.Recurring.active.is_(True))
                    .order_by(models.Recurring.name).all()]
    # доп. данные для переклассификации (для transfer / без категории)
    accs_other = [{"id": a.id, "name": a.name, "owner": a.owner, "currency": a.currency}
                  for a in db.query(models.Account)
                  .filter(models.Account.archived.is_(False), models.Account.id != t.account_id).all()]
    open_debts = [{"id": d.id, "counterparty": d.counterparty, "direction": d.direction,
                   "remaining": round(max((d.amount or 0) - (d.paid or 0), 0), 2),
                   "currency": d.currency}
                  for d in db.query(models.Debt).filter(models.Debt.status == "open").all()]
    return {"id": t.id, "merchant": t.merchant, "amount": t.amount, "currency": t.currency,
            "dt": t.datetime.isoformat(), "type": t.type, "source": t.source, "note": t.note,
            "category_id": t.category_id, "category": cname(t.category_id), "items": items,
            "recurring_id": t.recurring_id, "sources": src_list,
            "account_id": t.account_id, "counterparty_account_id": t.counterparty_account_id,
            "review": ((t.status == "needs_review") or
                       (t.type in ("expense", "income") and not t.category_id) or
                       (t.type == "transfer" and not t.counterparty_account_id)),
            "accounts_other": accs_other, "open_debts": open_debts}


class ReclassifyIn(BaseModel):
    kind: str   # purchase | self_transfer | give_loan | repay_to_me | repay_my_debt
    category_id: int | None = None
    counterparty_account_id: int | None = None
    counterparty_name: str | None = None
    debt_id: int | None = None


@app.post("/api/tx/{tx_id}/reclassify")
async def reclassify_tx(tx_id: int, body: ReclassifyIn, user: dict = Depends(current_user),
                         db: Session = Depends(get_session)):
    """Переклассификация операции: покупка / перевод между счетами / в долг / возврат долга."""
    t = db.get(models.Transaction, tx_id)
    if not t:
        raise HTTPException(404, "no tx")
    kind = body.kind
    if kind in ("expense", "purchase"):
        # это была покупка/расход (даже если в выписке выглядит как СБП-перевод)
        t.type = "expense"
        if body.category_id:
            t.category_id = body.category_id
            learn_rule(db, body.category_id, inn=None, pattern=t.merchant)
        t.counterparty_account_id = None
        t.status = "confirmed"
    elif kind == "income":
        # это был доход (зарплата, кешбэк, возврат от продавца)
        t.type = "income"
        if body.category_id:
            t.category_id = body.category_id
            learn_rule(db, body.category_id, inn=None, pattern=t.merchant)
        t.counterparty_account_id = None
        t.status = "confirmed"
    elif kind == "self_transfer":
        # перевод между нашими счетами (свой счёт или счёт жены)
        if not body.counterparty_account_id:
            raise HTTPException(400, "counterparty_account_id required")
        acc = db.get(models.Account, body.counterparty_account_id)
        if not acc:
            raise HTTPException(404, "no counterparty account")
        t.type = "transfer"
        t.counterparty_account_id = body.counterparty_account_id
        t.category_id = None
        t.status = "confirmed"
    elif kind == "give_loan":
        # я кому-то дал в долг (transfer + создание Debt с direction='owed_to_me')
        name = (body.counterparty_name or t.merchant or "").strip()[:128]
        if not name:
            raise HTTPException(400, "counterparty name required")
        d = models.Debt(counterparty=name, direction="owed_to_me",
                        amount=abs(t.amount), currency=t.currency or "RUB",
                        date=t.datetime.date(), status="open")
        db.add(d)
        t.type = "transfer"
        t.category_id = None
        t.status = "confirmed"
    elif kind == "repay_to_me":
        # мне вернули долг (приход) — закрываем существующий Debt 'owed_to_me'
        if not body.debt_id:
            raise HTTPException(400, "debt_id required")
        d = db.get(models.Debt, body.debt_id)
        if not d or d.direction != "owed_to_me":
            raise HTTPException(404, "no debt")
        d.paid = (d.paid or 0) + abs(t.amount)
        if d.paid >= (d.amount or 0):
            d.status = "closed"
        t.type = "income"
        t.category_id = None
        t.status = "confirmed"
    elif kind == "repay_my_debt":
        # я погасил свой долг (расход) — закрываем существующий Debt 'i_owe'
        if not body.debt_id:
            raise HTTPException(400, "debt_id required")
        d = db.get(models.Debt, body.debt_id)
        if not d or d.direction != "i_owe":
            raise HTTPException(404, "no debt")
        d.paid = (d.paid or 0) + abs(t.amount)
        if d.paid >= (d.amount or 0):
            d.status = "closed"
        t.type = "transfer"
        t.category_id = None
        t.status = "confirmed"
    else:
        raise HTTPException(400, f"unknown kind: {kind}")
    db.commit()
    return {"ok": True}


class CatIn(BaseModel):
    category_id: int


class BulkCatIn(BaseModel):
    ids: list[int]
    category_id: int


@app.post("/api/transactions/bulk")
async def bulk_set_category(body: BulkCatIn, user: dict = Depends(current_user),
                            db: Session = Depends(get_session)):
    """Массовая правка категории у нескольких операций (+ обучение правил)."""
    n = 0
    for tx_id in body.ids:
        t = db.get(models.Transaction, tx_id)
        if not t:
            continue
        t.category_id = body.category_id
        t.status = "confirmed"
        n += 1
        learn_rule(db, body.category_id,
                   inn=(t.receipt.inn if t.receipt else None), pattern=t.merchant)
    db.commit()
    return {"ok": True, "updated": n}


@app.post("/api/tx/{tx_id}")
async def set_tx_category(tx_id: int, body: CatIn,
                          user: dict = Depends(current_user), db: Session = Depends(get_session)):
    t = db.get(models.Transaction, tx_id)
    if not t:
        raise HTTPException(404, "no tx")
    t.category_id = body.category_id
    t.status = "confirmed"
    db.commit()
    learn_rule(db, body.category_id, inn=(t.receipt.inn if t.receipt else None), pattern=t.merchant)
    return {"ok": True}


class SourceIn(BaseModel):
    recurring_id: int | None = None


@app.post("/api/tx/{tx_id}/source")
async def set_tx_source(tx_id: int, body: SourceIn,
                        user: dict = Depends(current_user), db: Session = Depends(get_session)):
    """Привязать доходную операцию к источнику (Recurring income) + запомнить алиас."""
    t = db.get(models.Transaction, tx_id)
    if not t:
        raise HTTPException(404, "no tx")
    t.recurring_id = body.recurring_id
    db.commit()
    if body.recurring_id:
        learn_income_alias(db, t.merchant, t.note, body.recurring_id)
    return {"ok": True}


@app.post("/api/items/{item_id}")
async def set_item_category(item_id: int, body: CatIn,
                            user: dict = Depends(current_user), db: Session = Depends(get_session)):
    it = db.get(models.TransactionItem, item_id)
    if not it:
        raise HTTPException(404, "no item")
    it.category_id = body.category_id
    db.commit()
    tx = db.get(models.Transaction, it.transaction_id)
    if tx:
        _recompute_tx_category(tx)
        db.commit()
        learn_rule(db, body.category_id,
                   inn=(tx.receipt.inn if tx.receipt else None), pattern=it.name)
    return {"ok": True}


class TxIn(BaseModel):
    type: str = "expense"
    amount: float
    currency: str = "RUB"
    category_id: int | None = None
    account_id: int | None = None
    counterparty_account_id: int | None = None
    merchant: str | None = None
    note: str | None = None
    dt: str | None = None


@app.post("/api/tx")
async def create_tx(body: TxIn, user: dict = Depends(current_user),
                    db: Session = Depends(get_session)):
    """Ручное добавление операции из мини-аппа (напр. доход в крипте).
    В отличие от импорта выписки — balance затронутых счетов меняется
    автоматически (пользователь явно говорит «вот пришли деньги»), и
    сегодняшний snapshot пересчитывается."""
    ttype = body.type if body.type in ("expense", "income", "transfer") else "expense"
    cur = (body.currency or "RUB").upper()
    amt = abs(body.amount)
    base = to_rub(amt, cur, db)
    when = datetime.now()
    if body.dt:
        try:
            when = datetime.fromisoformat(body.dt)
        except Exception:  # noqa: BLE001
            pass
    t = models.Transaction(
        type=ttype, amount=amt, currency=cur, base_amount_rub=base,
        fx_rate=(base / amt if amt else 1.0),
        category_id=body.category_id, account_id=body.account_id,
        counterparty_account_id=body.counterparty_account_id,
        merchant=(body.merchant or None), note=(body.note or None),
        datetime=when, source="manual", status="confirmed",
    )
    db.add(t)
    # автокоррекция balance счетов
    if body.account_id:
        acc = db.get(models.Account, body.account_id)
        if acc:
            if ttype == "income":
                acc.balance = round((acc.balance or 0) + amt, 2)
            elif ttype == "expense":
                acc.balance = round((acc.balance or 0) - amt, 2)
            elif ttype == "transfer":
                acc.balance = round((acc.balance or 0) - amt, 2)
                if body.counterparty_account_id:
                    cp = db.get(models.Account, body.counterparty_account_id)
                    if cp:
                        cp.balance = round((cp.balance or 0) + amt, 2)
    db.commit()
    # обновим сегодняшний snapshot — мы только что изменили balance
    if body.account_id:
        take_networth_snapshot(db, force=True)
    return {"id": t.id}


class NoteIn(BaseModel):
    note: str | None = None


@app.post("/api/tx/{tx_id}/note")
async def set_tx_note(tx_id: int, body: NoteIn,
                      user: dict = Depends(current_user), db: Session = Depends(get_session)):
    t = db.get(models.Transaction, tx_id)
    if not t:
        raise HTTPException(404, "no tx")
    t.note = (body.note or "").strip()[:500] or None
    db.commit()
    return {"ok": True}


# Статика мини-аппа — ПОСЛЕ всех /api и /webhook (mount на "/" перехватывает остальное).
_FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND.is_dir():
    @app.get("/")
    async def _index():
        # без кэша — иначе Telegram WebView держит старый JS после деплоя
        return FileResponse(str(_FRONTEND / "index.html"),
                            headers={"Cache-Control": "no-store, max-age=0"})

    app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="static")
