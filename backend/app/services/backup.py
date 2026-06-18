"""
Зашифрованный бэкап БД в Telegram (личка владельца).
Снимок SQLite (консистентный, через .backup) → gzip → шифрование (Fernet, ключ из BACKUP_PASSPHRASE) → документ в чат.
Восстановление: расшифровать паролем → gunzip → положить как money.db.
"""
from __future__ import annotations

import base64
import gzip
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from aiogram.types import BufferedInputFile
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from ..config import settings
from ..db import SessionLocal
from .settings_store import get_setting, set_setting

log = logging.getLogger("money.backup")
RETENTION = 14   # держим последние N бэкапов в чате; старшие удаляем


def _fernet(passphrase: str) -> Fernet:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=b"money-backup-v1", iterations=200_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode())))


def _db_path() -> Path:
    url = settings.database_url
    return Path(url.split("///", 1)[-1]) if url.startswith("sqlite") else Path("")


def _snapshot() -> bytes:
    src_path = _db_path()
    tmp = src_path.parent / ".backup.tmp.db"
    src = sqlite3.connect(str(src_path))
    dst = sqlite3.connect(str(tmp))
    try:
        src.backup(dst)   # консистентный снимок, включая WAL
    finally:
        dst.close()
        src.close()
    data = tmp.read_bytes()
    tmp.unlink(missing_ok=True)
    return data


async def make_and_send_backup() -> bool:
    from ..bot import bot  # ленивый импорт — без циклической зависимости
    if not bot or not settings.backup_chat_id:
        return False
    p = _db_path()
    if not p.exists():
        log.warning("нет файла БД для бэкапа: %s", p)
        return False
    blob = gzip.compress(_snapshot())
    if settings.backup_passphrase:
        blob = _fernet(settings.backup_passphrase).encrypt(blob)
        ext = "db.gz.enc"
    else:
        ext = "db.gz"
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    chat_id = int(settings.backup_chat_id)
    msg = await bot.send_document(
        chat_id, BufferedInputFile(blob, filename=f"money_{stamp}.{ext}"),
        caption=f"Бэкап БД · {datetime.now().strftime('%d.%m.%Y %H:%M')}",
    )
    # ротация: держим N последних, старые удаляем
    db = SessionLocal()
    try:
        ids = json.loads(get_setting(db, "backup_msg_ids") or "[]")
        ids.append(msg.message_id)
        while len(ids) > RETENTION:
            old = ids.pop(0)
            try:
                await bot.delete_message(chat_id, old)
            except Exception:  # noqa: BLE001
                pass
        set_setting(db, "backup_msg_ids", json.dumps(ids))
    finally:
        db.close()
    return True
