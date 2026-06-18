"""
Telegram-бот (aiogram, webhook). Принимает фото чека / ценник / текст / голос,
разбирает и пишет в БД. Отвечает ТОЛЬКО владельцу (OWNER_TG_ID).
"""
from __future__ import annotations

import logging
from io import BytesIO

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from . import models
from .config import settings
from .db import SessionLocal
from .services import ingest
from .services.categorize import learn_rule
from .services.digests import build_daily, build_monthly, build_weekly

log = logging.getLogger("money.bot")
dp = Dispatcher()
bot: Bot | None = Bot(settings.bot_token) if settings.bot_token else None


def _owner(obj) -> bool:
    u = getattr(obj, "from_user", None)
    return bool(u) and u.id == settings.owner_tg_id


async def _download(file_id: str) -> bytes:
    f = await bot.get_file(file_id)
    buf = BytesIO()
    await bot.download_file(f.file_path, buf)
    return buf.getvalue()


def _kb(res: dict) -> InlineKeyboardMarkup | None:
    if not res.get("tx_id"):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✏️ Категория", callback_data=f"editcat:{res['tx_id']}")]])


def _cat_keyboard(db, tx_id: int) -> InlineKeyboardMarkup:
    cats = (db.query(models.Category)
            .filter(models.Category.type == "expense", models.Category.archived.is_(False))
            .order_by(models.Category.name).all())
    rows, row = [], []
    for c in cats:
        row.append(InlineKeyboardButton(text=c.name, callback_data=f"setcat:{tx_id}:{c.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _reply(note: Message, res: dict) -> None:
    await note.edit_text(res.get("text") or "Готово", parse_mode="HTML", reply_markup=_kb(res))


# ---------- команды и приём ----------

@dp.message(CommandStart())
async def on_start(m: Message):
    if not _owner(m):
        return
    await m.answer(
        "Привет! Я твой финансовый помощник 💸\n\n"
        "Кидай:\n• 🧾 фото чека (с QR) — разберу по товарам\n"
        "• 📷 ценник/квитанцию — пойму нейросетью\n"
        "• ✍️ текст: «такси 300», «зарплата 135000»\n"
        "• 🎙️ голосовое — расшифрую\n\n"
        "Отчёты: /report (день) · /week · /month\n"
        "Дашборд — кнопка «💰 MONEY» слева от поля ввода.")


async def _send_report(m: Message, builder) -> None:
    db = SessionLocal()
    try:
        text = builder(db)
    finally:
        db.close()
    await m.answer(text, parse_mode="HTML")


@dp.message(Command("report", "day"))
async def on_report(m: Message):
    if _owner(m):
        await _send_report(m, build_daily)


@dp.message(Command("week"))
async def on_week(m: Message):
    if _owner(m):
        await _send_report(m, build_weekly)


@dp.message(Command("month"))
async def on_month(m: Message):
    if _owner(m):
        await _send_report(m, build_monthly)


@dp.message(F.photo)
async def on_photo(m: Message):
    if not _owner(m):
        return
    note = await m.answer("Обрабатываю фото… ⏳")
    try:
        data = await _download(m.photo[-1].file_id)
        await _reply(note, await ingest.ingest_photo(data))
    except Exception as e:  # noqa: BLE001
        log.exception("photo")
        await note.edit_text(f"⚠️ Не смог обработать фото: {e}")


@dp.message(F.document)
async def on_document(m: Message):
    if not _owner(m):
        return
    doc = m.document
    name = (doc.file_name or "").lower()
    mime = doc.mime_type or ""
    if mime.startswith("image/"):
        note = await m.answer("Обрабатываю фото… ⏳")
        try:
            await _reply(note, await ingest.ingest_photo(await _download(doc.file_id)))
        except Exception as e:  # noqa: BLE001
            log.exception("doc image")
            await note.edit_text(f"⚠️ Не смог обработать файл: {e}")
    elif name.endswith(".csv") or "csv" in mime:
        note = await m.answer("Импортирую выписку… ⏳")
        try:
            res = await ingest.import_statement(await _download(doc.file_id))
            await note.edit_text(res["text"], parse_mode="HTML")
        except Exception as e:  # noqa: BLE001
            log.exception("doc csv")
            await note.edit_text(f"⚠️ Не смог импортировать выписку: {e}")
    else:
        await m.answer("Пришли фото чека, CSV-выписку, текст или голосовое.")


@dp.message(F.voice | F.audio)
async def on_voice(m: Message):
    if not _owner(m):
        return
    note = await m.answer("Слушаю… 🎙️")
    try:
        fid = m.voice.file_id if m.voice else m.audio.file_id
        await _reply(note, await ingest.ingest_voice(await _download(fid)))
    except Exception as e:  # noqa: BLE001
        log.exception("voice")
        await note.edit_text(f"⚠️ Не разобрал голос: {e}")


@dp.message(F.text)
async def on_text(m: Message):
    if not _owner(m):
        return
    note = await m.answer("Записываю… ✍️")
    try:
        await _reply(note, await ingest.ingest_text(m.text))
    except Exception as e:  # noqa: BLE001
        log.exception("text")
        await note.edit_text(f"⚠️ Не понял запись: {e}")


# ---------- правка категории ----------

@dp.callback_query(F.data.startswith("editcat:"))
async def cb_editcat(cq: CallbackQuery):
    if not _owner(cq):
        return
    tx_id = int(cq.data.split(":")[1])
    db = SessionLocal()
    try:
        kb = _cat_keyboard(db, tx_id)
    finally:
        db.close()
    await cq.message.edit_reply_markup(reply_markup=kb)
    await cq.answer("Выбери категорию")


@dp.callback_query(F.data.startswith("setcat:"))
async def cb_setcat(cq: CallbackQuery):
    if not _owner(cq):
        return
    _, tx_id, cat_id = cq.data.split(":")
    tx_id, cat_id = int(tx_id), int(cat_id)
    db = SessionLocal()
    try:
        tx = db.get(models.Transaction, tx_id)
        if not tx:
            await cq.answer("Не найдено")
            return
        tx.category_id = cat_id
        tx.status = "confirmed"
        db.commit()
        inn = tx.receipt.inn if tx.receipt else None
        learn_rule(db, cat_id, inn=inn, pattern=tx.merchant)   # запоминаем правило
        cat = db.get(models.Category, cat_id)
        await cq.message.edit_reply_markup(reply_markup=None)
        await cq.answer(f"✅ {cat.name if cat else 'ок'} — запомнил")
    finally:
        db.close()
