"""
Точка входа: FastAPI (API мини-аппа) + Telegram webhook в одном процессе.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session
from aiogram.types import Update

from . import bot as botmod
from . import models  # noqa: F401  — регистрируем таблицы в metadata
from .config import settings
from .db import Base, engine, get_session
from .security import current_user
from .services.dashboard import get_dashboard
from .services.fx import to_rub
from .services.settings_store import get_setting, set_setting


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # dev: создаём таблицы автоматически. На проде — alembic.
    Base.metadata.create_all(bind=engine)
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
    yield
    if botmod.bot:
        await botmod.bot.session.close()


app = FastAPI(title="MONEY", lifespan=lifespan)


@app.get("/api/health")
async def health():
    return {"ok": True, "env": settings.app_env}


@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    """Проверка авторизации мини-аппа (только владелец)."""
    return {"user": user}


@app.get("/api/dashboard")
async def dashboard(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    """Сводка для дашборда (только владелец)."""
    return get_dashboard(db)


@app.get("/api/accounts")
async def accounts(user: dict = Depends(current_user), db: Session = Depends(get_session)):
    rows = (db.query(models.Account).filter(models.Account.archived.is_(False))
            .order_by(models.Account.owner, models.Account.name).all())
    out = [{"id": a.id, "name": a.name, "type": a.type, "currency": a.currency,
            "owner": a.owner, "balance": a.balance,
            "rub": to_rub(a.balance, a.currency, db)} for a in rows]
    return {"accounts": out, "net_worth": round(sum(x["rub"] for x in out), 2)}


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


# Статика мини-аппа — ПОСЛЕ всех /api и /webhook (mount на "/" перехватывает остальное).
_FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="static")
