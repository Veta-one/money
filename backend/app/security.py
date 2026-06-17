"""
Безопасность входа в мини-апп — единственный механизм аутентификации.

Telegram WebApp передаёт `initData` (подписанную строку). Проверяем подпись
ботовым токеном (никто, кроме Telegram, её не подделает) и пускаем ТОЛЬКО
владельца (OWNER_TG_ID). Паролей нет — личность даёт Telegram.

Док: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException

from .config import settings

_MAX_AGE = 24 * 3600  # initData старше суток считаем протухшей (защита от replay)


def validate_init_data(init_data: str) -> dict:
    """Проверяет подпись initData и возвращает payload (с полем user). Иначе HTTPException."""
    if not settings.bot_token:
        raise HTTPException(503, "bot token not configured")
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        raise HTTPException(401, "malformed initData")

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "no hash in initData")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calc_hash, received_hash):
        raise HTTPException(401, "bad initData signature")

    auth_date = int(pairs.get("auth_date", "0"))
    if _MAX_AGE and (time.time() - auth_date) > _MAX_AGE:
        raise HTTPException(401, "initData expired")

    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(401, "bad user payload")
    return {"user": user, "auth_date": auth_date}


async def current_user(x_telegram_init_data: str = Header(default="")) -> dict:
    """
    FastAPI dependency. Заголовок X-Telegram-Init-Data = window.Telegram.WebApp.initData.
    Пускаем только владельца.
    """
    if not x_telegram_init_data:
        raise HTTPException(401, "no init data")
    payload = validate_init_data(x_telegram_init_data)
    uid = payload["user"].get("id")
    if uid != settings.owner_tg_id:
        raise HTTPException(403, "not the owner")
    return payload["user"]
