"""
Клиент к API ФНС «Мои чеки онлайн» (lkdr.nalog.ru). Перенесён с прототипа на
httpx, путь к токенам и прокси берутся из настроек.

Логин по SMS делается вручную в браузере (капча + RU IP), сюда импортируются
токены (set_tokens). /scan и refresh работают с любого IP; при блокировке US
можно задать FNS_PROXY (RU-прокси) — он применится ко всем запросам.
Полное описание схемы API — в памяти проекта [[fns-lkdr-api]].
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..config import settings

BASE = "https://lkdr.nalog.ru/api"
START_URL = f"{BASE}/v2/auth/challenge/phone/start"
VERIFY_URL = f"{BASE}/v1/auth/challenge/phone/verify"
REFRESH_URL = f"{BASE}/v1/auth/token"
SCAN_URL = f"{BASE}/v1/scan"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _nanoid(n: int = 21) -> str:
    alphabet = "useandom-26T198340PX75pxJACKVERYMINDBUSHWOLF_GQZbfghjklqvwyzrict"
    return "".join(secrets.choice(alphabet) for _ in range(n))


class LkdrError(Exception):
    def __init__(self, code: str, message: str | None, additional: object = None):
        super().__init__(f"[{code}] {message or ''}".strip())
        self.code, self.message, self.additional = code, message, additional


def _parse(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        raise LkdrError("HTTP", f"{resp.status_code}: {resp.text[:200]}")
    if isinstance(data, dict):
        if "code" in data:
            raise LkdrError(data["code"], data.get("message"), data.get("additionalInfo"))
        if "operationId" in data and "message" in data and resp.status_code >= 400:
            raise LkdrError(f"HTTP{resp.status_code}", data.get("message"))
    return data


class LkdrClient:
    def __init__(self, tokens_path: str | None = None, proxy: str | None = None):
        self.tokens_path = Path(tokens_path or settings.fns_tokens_path)
        self.proxy = proxy if proxy is not None else settings.fns_proxy
        self.token: str | None = None
        self.refresh_token: str | None = None
        self.phone: str | None = None
        self.source_device_id: str | None = None
        self._challenge_token: str | None = None
        self._load()
        if not self.source_device_id:
            self.source_device_id = _nanoid()

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=30,
            proxy=self.proxy or None,
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                "Origin": "https://lkdr.nalog.ru",
                "Referer": "https://lkdr.nalog.ru/login",
            },
        )

    @property
    def device_info(self) -> dict:
        return {
            "sourceDeviceId": self.source_device_id,
            "sourceType": "WEB",
            "appVersion": "1.0.0",
            "metaDetails": {"userAgent": USER_AGENT},
        }

    # ---- хранение ----
    def _load(self) -> None:
        if self.tokens_path.exists():
            d = json.loads(self.tokens_path.read_text(encoding="utf-8"))
            self.token = d.get("token")
            self.refresh_token = d.get("refreshToken")
            self.phone = d.get("phone")
            self.source_device_id = d.get("sourceDeviceId")

    def _save(self) -> None:
        self.tokens_path.parent.mkdir(parents=True, exist_ok=True)
        self.tokens_path.write_text(json.dumps({
            "token": self.token, "refreshToken": self.refresh_token,
            "phone": self.phone, "sourceDeviceId": self.source_device_id,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---- импорт токенов из браузера (обход капчи) ----
    def set_tokens(self, token: str, refresh_token: str | None = None,
                   source_device_id: str | None = None) -> None:
        self.token = token.strip()
        if refresh_token:
            self.refresh_token = refresh_token.strip()
        if source_device_id:
            self.source_device_id = source_device_id.strip()
        self._save()

    # ---- авторизация (нужен RU IP + captchaToken из браузера) ----
    def request_sms(self, phone: str, captcha_token: str) -> dict:
        self.phone = _normalize_phone(phone)
        with self._client() as c:
            data = _parse(c.post(START_URL, json={
                "phone": self.phone, "captchaToken": captcha_token,
                "deviceInfo": self.device_info}))
        self._challenge_token = data["challengeToken"]
        return data

    def submit_sms_code(self, code: str) -> dict:
        with self._client() as c:
            data = _parse(c.post(VERIFY_URL, json={
                "challengeToken": self._challenge_token, "phone": self.phone,
                "code": str(code).strip(), "deviceInfo": self.device_info}))
        self._apply_tokens(data)
        return data

    def refresh(self) -> dict:
        if not self.refresh_token:
            raise RuntimeError("нет refreshToken")
        with self._client() as c:
            data = _parse(c.post(REFRESH_URL, json={
                "refreshToken": self.refresh_token, "deviceInfo": self.device_info}))
        self._apply_tokens(data)
        return data

    def _apply_tokens(self, data: dict) -> None:
        self.token = data.get("token", self.token)
        self.refresh_token = data.get("refreshToken", self.refresh_token)
        self._save()

    # ---- получение чека ----
    @staticmethod
    def qr_to_scan_payload(qr: dict[str, str]) -> dict:
        t = qr["t"]
        fmt = "%Y%m%dT%H%M" if len(t) == 13 else "%Y%m%dT%H%M%S"
        dt = datetime.strptime(t, fmt)
        return {
            "createdDate": dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "fiscalDocumentNumber": str(qr["i"]),
            "fiscalDriveNumber": str(qr["fn"]),
            "fiscalSign": str(qr["fp"]),
            "operationType": int(qr["n"]),
            "scanDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "totalSum": f"{float(qr['s']):.2f}",
        }

    def scan(self, qr: dict[str, str], _retried: bool = False) -> dict:
        if not self.token:
            raise RuntimeError("нет access-токена")
        with self._client() as c:
            resp = c.post(SCAN_URL, json=self.qr_to_scan_payload(qr),
                          headers={"Authorization": f"Bearer {self.token}"})
        try:
            return _parse(resp)
        except LkdrError as e:
            if not _retried and e.code == "authentication.failed" and self.refresh_token:
                self.refresh()
                return self.scan(qr, _retried=True)
            raise


def _normalize_phone(phone: str) -> str:
    p = "".join(ch for ch in phone if ch.isdigit())
    if p.startswith("8"):
        p = "7" + p[1:]
    if not p.startswith("7"):
        p = "7" + p
    return p
