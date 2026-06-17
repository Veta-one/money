"""
Нормализация ответа ФНС /scan в удобный чек. Устойчиво к вложенности:
рекурсивно ищем словарь с ключом items. Фискальные суммы — в копейках (int),
приводим к рублям делением на 100.
"""
from __future__ import annotations

from datetime import datetime


def _find_receipt(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("items"), list) and obj["items"]:
            return obj
        for v in obj.values():
            r = _find_receipt(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_receipt(v)
            if r:
                return r
    return None


def _money(v) -> float:
    try:
        return round(float(v) / 100, 2)  # копейки -> рубли
    except (TypeError, ValueError):
        return 0.0


def _parse_dt(v) -> datetime:
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v)
        except (OverflowError, OSError, ValueError):
            return datetime.now()
    if isinstance(v, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                return datetime.strptime(v[:19], fmt)
            except ValueError:
                continue
    return datetime.now()


def parse_scan(scan: dict) -> dict | None:
    rec = _find_receipt(scan)
    if not rec:
        return None
    items = [{
        "name": (it.get("name") or "").strip(),
        "quantity": float(it.get("quantity") or 1),
        "price": _money(it.get("price")),
        "sum": _money(it.get("sum")),
    } for it in rec.get("items", [])]
    return {
        "merchant": (rec.get("user") or rec.get("retailPlace") or "").strip(),
        "inn": str(rec.get("userInn") or "").strip(),
        "retail_place": (rec.get("retailPlace") or rec.get("retailPlaceAddress") or "").strip(),
        "datetime": _parse_dt(rec.get("dateTime")),
        "total": _money(rec.get("totalSum")),
        "items": items,
    }
