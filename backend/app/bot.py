"""
Telegram-бот (aiogram, webhook). Отвечает ТОЛЬКО владельцу (OWNER_TG_ID),
всех остальных молча игнорирует. Полная обработка ввода — Фаза 1.
"""
from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

from .config import settings

dp = Dispatcher()
bot: Bot | None = Bot(settings.bot_token) if settings.bot_token else None


def _is_owner(m: Message) -> bool:
    return bool(m.from_user) and m.from_user.id == settings.owner_tg_id


@dp.message(CommandStart())
async def on_start(m: Message) -> None:
    if not _is_owner(m):
        return
    await m.answer("Привет! Я твой финансовый помощник 💸\n"
                   "Кидай фото чека, текст или голосовое — со временем разберу всё сам.")


@dp.message()
async def on_any(m: Message) -> None:
    if not _is_owner(m):
        return
    await m.answer("Принял ✅ Полная обработка ввода появится на Фазе 1.")
