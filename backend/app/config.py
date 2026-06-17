"""Конфигурация из .env (pydantic-settings). Секретов в коде нет."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    app_env: str = "dev"
    timezone: str = "Europe/Moscow"

    # Telegram
    bot_token: str = ""
    owner_tg_id: int = 0
    webhook_secret: str = ""
    public_url: str = ""

    # БД
    database_url: str = "sqlite:///./data/money.db"

    # ФНС
    fns_tokens_path: str = "./data/tokens.json"
    fns_proxy: str | None = None

    # LLM (OpenRouter)
    openrouter_keys: str = ""           # бесплатные ключи через запятую
    openrouter_paid_key: str = ""
    gemini_model: str = "google/gemini-2.0-flash-exp:free"
    paid_model: str = "google/gemini-2.5-pro"

    # Бэкап
    backup_chat_id: str = ""
    backup_passphrase: str = ""

    # Финансы
    base_currency: str = "RUB"
    expected_monthly_income: float = 0.0

    @property
    def openrouter_key_list(self) -> list[str]:
        return [k.strip() for k in self.openrouter_keys.split(",") if k.strip()]


settings = Settings()
