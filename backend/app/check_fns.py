"""
Проверка доступности ФНС с текущего IP — ГЛАВНЫЙ техриск Фазы 0.
Запусти НА VPS:  python -m app.check_fns   (или docker compose exec app python -m app.check_fns)

Если печатает 'blocked.ip' — IP заблокирован, задай FNS_PROXY (RU-прокси) в .env.
"""
from __future__ import annotations

from .services.fns import LkdrClient, LkdrError

# реквизиты одного из реальных чеков (Магнит) — только для проверки доступа
SAMPLE_QR = {"t": "20260617T2107", "s": "323.96", "fn": "7382440900295083",
             "i": "6681", "fp": "712538130", "n": "1"}


def main() -> None:
    c = LkdrClient()
    print("Токены:", c.tokens_path, "| прокси:", c.proxy or "нет")
    if not c.token:
        print("Нет access-токена — сначала импортируй (set_tokens).")
        return
    try:
        data = c.scan(SAMPLE_QR)
        shop = data.get("receipt", {}).get("user")
        print(f"OK ✅ /scan работает с этого IP. Магазин из чека: {shop}")
    except LkdrError as e:
        if e.code == "blocked.ip":
            print("❌ blocked.ip — нужен RU-прокси: задай FNS_PROXY в .env")
        else:
            print(f"Ошибка ФНС: {e}")


if __name__ == "__main__":
    main()
