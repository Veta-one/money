"""
Парсер банковской выписки Райффайзена (CSV).
Колонки: Дата операции, ..., Номер документа, ...(поступления)/(расходы) в валюте счёта,
Валюта счёта, Детали операции (продавец), Номер карты.
Суммы в формате "10 000,00" (пробел-разделитель тысяч, запятая-десятичная).
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

# Переводы/служебное — не считаем тратами (тип transfer).
_TRANSFER_HINTS = ("сбп", "перевод", "payment", "со счета", "со счёта",
                   "идентификатор операции", "p2p", "пополнение", "выдача наличных")


def _num(s: str) -> float:
    s = (s or "").replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def _dt(s: str) -> datetime:
    s = (s or "").strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.now()


def _col(row: dict, *needles: str) -> str:
    """Находит значение по подстрокам в названии колонки (устойчиво к мелким отличиям)."""
    for k, v in row.items():
        kl = (k or "").lower()
        if all(n in kl for n in needles):
            return (v or "").strip()
    return ""


def _sniff_delimiter(sample: str) -> str:
    """Авто-детект разделителя CSV: Райф/1С даёт ';', Google Sheets — ','."""
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        # фоллбэк: считаем в первой строке
        first = sample.splitlines()[0] if sample else ""
        return ";" if first.count(";") > first.count(",") else ","


def parse_statement(content: bytes) -> list[dict]:
    text = None
    for enc in ("utf-8-sig", "cp1251", "utf-8"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = content.decode("utf-8", errors="replace")
    delimiter = _sniff_delimiter(text[:4096])
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    out: list[dict] = []
    for row in reader:
        income = _num(_col(row, "счет", "поступлен")) or _num(_col(row, "счёт", "поступлен"))
        expense = _num(_col(row, "счет", "расход")) or _num(_col(row, "счёт", "расход"))
        if income <= 0 and expense <= 0:
            continue
        merchant = _col(row, "детали") or _col(row, "назначен")
        doc = _col(row, "номер документа")
        currency = _col(row, "валюта счет") or _col(row, "валюта счёт") or "RUB"
        when = _dt(_col(row, "дата операц"))
        ttype = "income" if income > 0 else "expense"
        amount = income if income > 0 else expense
        # Heuristic «перевод» применяем ТОЛЬКО к расходам — приход «перевод» это
        # обычно деньги от кого-то (доход), не transfer между нашими счетами.
        if ttype == "expense" and any(h in merchant.lower() for h in _TRANSFER_HINTS):
            ttype = "transfer"
        out.append({"doc": doc, "datetime": when, "amount": round(amount, 2),
                    "type": ttype, "merchant": merchant, "currency": currency})
    return out
