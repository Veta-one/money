"""
LLM-слой.

- Бесплатный Gemini (прямые ключи Google AI) — голос, картинки, текст, категоризация.
  Ротация ключей срабатывает ТОЛЬКО на HTTP 429 (упёрлись в лимит ключа):
  round-robin старт + переход к следующему ключу, пока кто-то не ответит.
- OpenRouter (платный ключ) — сложные задачи/аналитика.
"""
from __future__ import annotations

import base64
import logging

import httpx

from ..config import settings

log = logging.getLogger("money.llm")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# 429 — лимит ключа; 5xx — модель перегружена/недоступна. И то и другое → пробуем следующий ключ.
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class GeminiPool:
    """Пул ключей Google AI: round-robin + переключение на следующий ключ при 429."""

    def __init__(self, keys: list[str], model: str):
        self.keys = keys
        self.model = model
        self._i = 0

    def _key_order(self) -> list[str]:
        if not self.keys:
            raise RuntimeError("GEMINI_KEYS не заданы")
        start = self._i % len(self.keys)
        self._i = (self._i + 1) % len(self.keys)   # следующий вызов начнёт с другого ключа
        return self.keys[start:] + self.keys[:start]

    async def _generate(self, parts: list[dict], model: str | None = None) -> str:
        model = model or self.model
        body = {"contents": [{"parts": parts}]}
        last_status: int | None = None
        async with httpx.AsyncClient(timeout=120) as cli:
            for key in self._key_order():
                r = await cli.post(
                    f"{GEMINI_BASE}/models/{model}:generateContent",
                    params={"key": key}, json=body,
                )
                if r.status_code in _RETRY_STATUSES:   # лимит/перегрузка — следующий ключ
                    last_status = r.status_code
                    continue
                r.raise_for_status()
                cands = r.json().get("candidates") or []
                if not cands:
                    raise RuntimeError("Gemini вернул пустой ответ")
                return "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))
        raise RuntimeError(f"Gemini недоступен по всем ключам (последний статус {last_status})")

    async def text(self, prompt: str, model: str | None = None) -> str:
        return await self._generate([{"text": prompt}], model)

    async def transcribe(self, audio: bytes, mime: str = "audio/ogg",
                         prompt: str = "Расшифруй голосовое сообщение дословно. Верни только текст.") -> str:
        return await self._generate([
            {"text": prompt},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode()}},
        ])

    async def vision(self, image: bytes, prompt: str, mime: str = "image/jpeg") -> str:
        return await self._generate([
            {"text": prompt},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode()}},
        ])


async def openrouter_chat(messages: list[dict], model: str | None = None) -> str:
    """Сложные задачи через OpenRouter (платный ключ)."""
    if not settings.openrouter_paid_key:
        raise RuntimeError("OPENROUTER_PAID_KEY не задан")
    async with httpx.AsyncClient(timeout=120) as cli:
        r = await cli.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {settings.openrouter_paid_key}"},
            json={"model": model or settings.paid_model, "messages": messages},
        )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# Готовый пул для импорта из остального кода (Фаза 1).
gemini = GeminiPool(settings.gemini_key_list, settings.gemini_model)
