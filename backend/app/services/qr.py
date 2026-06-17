"""
Распознавание QR с фото чеков (opencv + pyzbar + предобработки) и парсинг
строки ФНС. Перенесено из прототипа qr_decoder.py, логика та же.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qsl

import cv2
import numpy as np

try:
    from pyzbar.pyzbar import decode as zbar_decode
    HAS_ZBAR = True
except Exception:
    HAS_ZBAR = False


def _variants(img_bgr: np.ndarray):
    yield "orig", img_bgr
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    yield "gray", gray
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
    yield "clahe", clahe
    _, otsu = cv2.threshold(clahe, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield "otsu", otsu
    yield "adaptive", cv2.adaptiveThreshold(
        clahe, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5)
    for scale in (1.5, 2.0, 3.0):
        h, w = img_bgr.shape[:2]
        yield f"resize_{scale}", cv2.resize(
            img_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
    for angle, code in [(90, cv2.ROTATE_90_CLOCKWISE), (180, cv2.ROTATE_180),
                        (270, cv2.ROTATE_90_COUNTERCLOCKWISE)]:
        yield f"rot{angle}", cv2.rotate(img_bgr, code)


def _try_opencv(img) -> list[str]:
    det = cv2.QRCodeDetector()
    out: list[str] = []
    try:
        ok, decoded, _, _ = det.detectAndDecodeMulti(img)
        if ok:
            out.extend(s for s in decoded if s)
    except cv2.error:
        pass
    if not out:
        data, _, _ = det.detectAndDecode(img)
        if data:
            out.append(data)
    return out


def _try_zbar(img) -> list[str]:
    if not HAS_ZBAR:
        return []
    try:
        return [r.data.decode("utf-8", "replace") for r in zbar_decode(img) if r.data]
    except Exception:
        return []


def decode_qrs_from_bytes(data: bytes) -> list[str]:
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return []
    return _decode(img)


def decode_qrs(path: str | Path) -> list[str]:
    img = cv2.imread(str(path))
    if img is None:
        return []
    return _decode(img)


def _decode(img) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for _name, variant in _variants(img):
        for s in (*_try_opencv(variant), *_try_zbar(variant)):
            if s and s not in seen:
                seen.add(s)
                found.append(s)
    return found


def parse_fns_qr(qr: str) -> dict[str, str] | None:
    """Строка t=...&s=...&fn=...&i=...&fp=...&n=... → dict, или None."""
    if "fn=" not in qr or "fp=" not in qr or "i=" not in qr:
        return None
    pairs = dict(parse_qsl(qr, keep_blank_values=True))
    if not {"t", "s", "fn", "i", "fp", "n"}.issubset(pairs):
        return None
    return pairs
