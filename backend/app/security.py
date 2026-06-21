"""
Безопасность входа в мини-апп — единственный механизм аутентификации.

Telegram WebApp передаёт `initData` (подписанную строку). Проверяем подпись
ботовым токеном (никто, кроме Telegram, её не подделает) и пускаем ТОЛЬКО
владельца (OWNER_TG_ID). Паролей нет — личность даёт Telegram.

Док: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Cookie, Header, HTTPException

from .config import settings

_MAX_AGE = 24 * 3600  # initData старше суток считаем протухшей (защита от replay)
SESSION_COOKIE = "money_session"
_SESSION_DAYS = 30


def _session_secret() -> bytes:
    return (settings.webhook_secret or settings.bot_token or "money").encode()


def make_session_token(uid: int, name: str = "", days: int = _SESSION_DAYS) -> str:
    """Подписанный токен браузерной сессии (после входа через Telegram Login Widget)."""
    exp = int(time.time()) + days * 86400
    nb = base64.urlsafe_b64encode((name or "").encode()).decode()
    payload = f"{uid}:{nb}:{exp}"
    sig = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_session_token(token: str) -> dict | None:
    """Проверяет подпись и срок сессионного токена → {id, first_name} или None."""
    try:
        uid_s, nb, exp_s, sig = token.rsplit(":", 3)
        payload = f"{uid_s}:{nb}:{exp_s}"
        calc = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, sig):
            return None
        if int(exp_s) < time.time():
            return None
        name = base64.urlsafe_b64decode(nb.encode()).decode()
        return {"id": int(uid_s), "first_name": name}
    except Exception:  # noqa: BLE001
        return None


def validate_login_widget(data: dict) -> dict:
    """Проверяет подпись данных Telegram Login Widget (вход на сайте в браузере).

    Отличие от initData: secret_key = SHA256(bot_token) (а не HMAC «WebAppData»).
    Док: https://core.telegram.org/widgets/login#checking-authorization
    """
    if not settings.bot_token:
        raise HTTPException(503, "bot token not configured")
    received_hash = data.get("hash")
    if not received_hash:
        raise HTTPException(401, "no hash")
    pairs = {k: str(v) for k, v in data.items() if k != "hash" and v is not None}
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hashlib.sha256(settings.bot_token.encode()).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, received_hash):
        raise HTTPException(401, "bad login signature")
    auth_date = int(data.get("auth_date", "0") or 0)
    if _MAX_AGE and (time.time() - auth_date) > _MAX_AGE:
        raise HTTPException(401, "login expired")
    return {"id": int(data.get("id")), "first_name": data.get("first_name", "")}


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


async def current_user(x_telegram_init_data: str = Header(default=""),
                       money_session: str = Cookie(default="")) -> dict:
    """
    FastAPI dependency. Два пути входа, оба пускают ТОЛЬКО владельца:
    1. Mini App — заголовок X-Telegram-Init-Data (window.Telegram.WebApp.initData).
    2. Браузер — cookie сессии после входа через Telegram Login Widget.
    """
    # 1. Telegram Mini App
    if x_telegram_init_data:
        payload = validate_init_data(x_telegram_init_data)
        uid = payload["user"].get("id")
        if uid != settings.owner_tg_id:
            raise HTTPException(403, "not the owner")
        return payload["user"]
    # 2. Браузерная сессия (Login Widget)
    if money_session:
        u = verify_session_token(money_session)
        if u and u["id"] == settings.owner_tg_id:
            return u
        raise HTTPException(401, "bad session")
    raise HTTPException(401, "no auth")
