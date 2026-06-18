"""
Оркестратор приёма: фото/текст/голос → транзакция(и) в БД.
Возвращает dict {status, text (HTML для ответа), tx_id?, review?}.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta

from .. import models
from ..db import SessionLocal
from . import receipt as receipt_parser
from .categorize import (categorize_items, categorize_one, category_by_name,
                         category_names, classify_texts, rule_category_id)
from .fns import LkdrClient, LkdrError
from .llm import gemini
from .qr import decode_qrs_from_bytes, parse_fns_qr
from .statement import parse_statement

_EMOJI = {"expense": "🧾", "income": "📥", "transfer": "🔄"}


def _default_account(db, cash: bool = False):
    name = "Наличные" if cash else "Райффайзен"
    acc = db.query(models.Account).filter(models.Account.name == name).first()
    return acc or db.query(models.Account).filter(models.Account.archived.is_(False)).first()


def _json_from(raw: str):
    m = re.search(r"(\{.*\}|\[.*\])", raw or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


# ---------- фото ----------

async def ingest_photo(data: bytes) -> dict:
    qrs = decode_qrs_from_bytes(data)
    fns_qr = next((q for q in (parse_fns_qr(s) for s in qrs) if q), None)
    if fns_qr:
        return await _ingest_receipt(fns_qr)
    return await _ingest_vision(data)


async def _ingest_receipt(qr: dict) -> dict:
    db = SessionLocal()
    try:
        if db.query(models.Receipt).filter_by(
                fn=str(qr["fn"]), fd=str(qr["i"]), fp=str(qr["fp"])).first():
            return {"status": "dup", "text": "🧾 Этот чек уже загружен."}
        client = LkdrClient()
        if not client.token:
            return {"status": "error",
                    "text": "🔑 На сервере нет токенов ФНС. Пришли свежие — и распознаю состав чека."}
        try:
            scan = await asyncio.to_thread(client.scan, qr)
        except LkdrError as e:
            if e.code == "authentication.failed":
                return {"status": "error", "text": "🔑 Токен ФНС протух — нужны свежие."}
            return {"status": "error", "text": f"ФНС: {e.message or e.code}"}
        parsed = receipt_parser.parse_scan(scan)
        if not parsed or not parsed["items"]:
            import logging
            logging.getLogger("money.ingest").warning("scan не распознан: %s",
                                                       json.dumps(scan, ensure_ascii=False)[:800])
            return {"status": "error", "text": "Чек получен, но не разобрал состав (структуру вижу в логах)."}

        acc = _default_account(db)
        tx = models.Transaction(
            account_id=acc.id if acc else None, datetime=parsed["datetime"],
            amount=parsed["total"], currency="RUB", base_amount_rub=parsed["total"],
            fx_rate=1.0, type="expense", merchant=parsed["merchant"] or parsed["retail_place"],
            source="receipt", status="confirmed", dedup_key=f"{qr['fn']}_{qr['i']}_{qr['fp']}",
        )
        db.add(tx)
        db.flush()
        db.add(models.Receipt(
            transaction_id=tx.id, fn=str(qr["fn"]), fd=str(qr["i"]), fp=str(qr["fp"]),
            t=str(qr["t"]), s=str(qr["s"]), n=int(qr["n"]),
            raw_qr="&".join(f"{k}={v}" for k, v in qr.items()),
            fns_json=json.dumps(scan, ensure_ascii=False)[:100000],
            kkt_owner=parsed["merchant"], inn=parsed["inn"], retail_place=parsed["retail_place"],
        ))
        cats = await categorize_items(db, parsed["items"], parsed["inn"])
        review = False
        for it, c in zip(parsed["items"], cats):
            db.add(models.TransactionItem(
                transaction_id=tx.id, name=it["name"], name_normalized=it["name"].lower(),
                qty=it["quantity"], price=it["price"], sum=it["sum"], category_id=c["category_id"]))
            review = review or c["needs_review"]
        tx.category_id = _dominant(parsed["items"], cats)
        db.commit()
        return {"status": "ok", "tx_id": tx.id, "review": review,
                "text": _receipt_text(db, parsed, cats)}
    finally:
        db.close()


def _dominant(items, cats):
    sums: dict[int, float] = {}
    for it, c in zip(items, cats):
        if c["category_id"]:
            sums[c["category_id"]] = sums.get(c["category_id"], 0.0) + it["sum"]
    return max(sums, key=sums.get) if sums else None


def _receipt_text(db, parsed, cats) -> str:
    lines = [f"🧾 <b>{parsed['merchant'] or 'Чек'}</b> — {parsed['total']:.2f} ₽",
             parsed["datetime"].strftime("%d.%m.%Y %H:%M"), ""]
    for it, c in zip(parsed["items"], cats):
        cn = "❓"
        if c["category_id"]:
            cobj = db.get(models.Category, c["category_id"])
            cn = cobj.name if cobj else "❓"
        lines.append(f"• {it['name'][:36]} — {it['sum']:.2f} ₽ <i>[{cn}]</i>")
    return "\n".join(lines)


# ---------- текст / голос / vision ----------

async def ingest_text(text: str) -> dict:
    db = SessionLocal()
    try:
        prompt = (
            "Разбери запись о личных финансах и верни ТОЛЬКО JSON.\n"
            'Поля: amount (число > 0), currency ("RUB" по умолчанию), '
            'type ("expense"|"income"|"transfer"), cash (true/false), '
            "merchant (на что/где, кратко), "
            f"category (одно из: {', '.join(category_names(db, ('expense',)))}; или null).\n"
            'Если запись не про деньги — верни {"amount": null}.\n\n'
            f"Запись: {text}"
        )
        data = _json_from(await gemini.text(prompt))
        if not data or not data.get("amount"):
            return {"status": "skip",
                    "text": "Не понял сумму. Напиши, например: «такси 300» или «зарплата 135000»."}
        return await _save_simple(db, data, source="text", raw=text)
    finally:
        db.close()


async def ingest_voice(data: bytes) -> dict:
    text = (await gemini.transcribe(data, mime="audio/ogg")).strip()
    res = await ingest_text(text)
    res["text"] = f"🎙️ «{text[:120]}»\n\n" + res.get("text", "")
    return res


async def import_statement(content: bytes) -> dict:
    """Импорт CSV-выписки: дедуп по номеру документа + склейка с чеками, категоризация."""
    db = SessionLocal()
    try:
        rows = parse_statement(content)
        if not rows:
            return {"status": "error", "text": "📄 Не нашёл операций. Нужен CSV-выписка из Райффайзена."}
        acc = _default_account(db)
        # категоризируем продавцов-расходы пакетом (правила → LLM)
        merchants = list({r["merchant"] for r in rows if r["type"] == "expense" and r["merchant"]})
        cat_map: dict[str, int | None] = {}
        unknown = []
        for mname in merchants:
            cid = rule_category_id(db, None, mname)
            if cid:
                cat_map[mname] = cid
            else:
                unknown.append(mname)
        if unknown:
            names = await classify_texts(unknown, category_names(db, ("expense",)))
            for mname, cname in zip(unknown, names):
                cobj = category_by_name(db, cname)
                cat_map[mname] = cobj.id if cobj else None

        imported = skipped = 0
        for r in rows:
            dk = f"stmt_{r['doc']}" if r["doc"] else f"stmt_{r['datetime'].isoformat()}_{r['amount']}"
            if db.query(models.Transaction).filter_by(dedup_key=dk).first():
                skipped += 1
                continue
            # склейка: если уже есть чек/операция на ту же сумму ±36ч — не дублируем
            lo, hi = r["datetime"] - timedelta(hours=36), r["datetime"] + timedelta(hours=36)
            cands = (db.query(models.Transaction)
                     .filter(models.Transaction.datetime >= lo, models.Transaction.datetime <= hi).all())
            if any(abs(c.base_amount_rub - r["amount"]) < 0.01 and c.source != "statement" for c in cands):
                skipped += 1
                continue
            db.add(models.Transaction(
                account_id=acc.id if acc else None, datetime=r["datetime"], amount=r["amount"],
                currency=r["currency"], base_amount_rub=r["amount"], fx_rate=1.0, type=r["type"],
                category_id=cat_map.get(r["merchant"]) if r["type"] == "expense" else None,
                merchant=r["merchant"][:256], source="statement", status="confirmed", dedup_key=dk))
            imported += 1
        db.commit()
        return {"status": "ok",
                "text": f"📄 Выписка импортирована\nДобавлено: <b>{imported}</b> · пропущено (дубли/склейка): {skipped} из {len(rows)}"}
    finally:
        db.close()


async def _ingest_vision(image: bytes) -> dict:
    db = SessionLocal()
    try:
        prompt = (
            "На фото — что-то про трату (ценник, квитанция, товар, записка). Верни ТОЛЬКО JSON: "
            "amount (число или null), merchant (что/где кратко), "
            f'type ("expense"), category (одно из: {", ".join(category_names(db, ("expense",)))}; или null).')
        data = _json_from(await gemini.vision(image, prompt))
        if not data or not data.get("amount"):
            return {"status": "skip",
                    "text": "📷 Не нашёл сумму на фото. Если это кассовый чек — захвати в кадр QR-код."}
        return await _save_simple(db, data, source="photo", raw="(фото)")
    finally:
        db.close()


async def _save_simple(db, data, source, raw) -> dict:
    amount = abs(float(data["amount"]))
    ttype = data.get("type") if data.get("type") in ("expense", "income", "transfer") else "expense"
    currency = (data.get("currency") or "RUB").upper()
    merchant = (data.get("merchant") or "").strip() or None
    cat_id, review = None, False
    cobj = category_by_name(db, data.get("category"))
    if cobj:
        cat_id = cobj.id
    elif ttype == "expense":
        cat_id, review = await categorize_one(db, merchant or raw)

    acc = _default_account(db, cash=bool(data.get("cash")))
    tx = models.Transaction(
        account_id=acc.id if acc else None, datetime=datetime.now(),
        amount=amount, currency=currency, base_amount_rub=amount, fx_rate=1.0,
        type=ttype, category_id=cat_id, merchant=merchant, source=source,
        status="needs_review" if review else "confirmed", note=str(raw)[:500],
    )
    db.add(tx)
    db.flush()
    cname = "❓"
    if cat_id:
        c = db.get(models.Category, cat_id)
        cname = c.name if c else "❓"
    tx_id = tx.id
    db.commit()
    sign = "+" if ttype == "income" else "−"
    cur = "₽" if currency == "RUB" else currency
    text = f"{_EMOJI[ttype]} {merchant or 'Операция'}\n{sign}{amount:.0f} {cur} <i>[{cname}]</i>"
    return {"status": "ok", "tx_id": tx_id, "review": review, "text": text}
