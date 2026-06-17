# MONEY — личный финансовый помощник

Telegram-бот (ввод: фото чеков / текст / голос) + веб мини-апп (дашборд) + аналитика.
Для одного пользователя. Подробная спецификация — в [SPEC.md](SPEC.md).

## Архитектура (кратко)
- **Backend** — Python, FastAPI + aiogram в одном процессе (бот через webhook).
- **БД** — SQLite (WAL) через SQLAlchemy ORM. Один пользователь → не нужен отдельный сервер БД; легко бэкапить; при необходимости меняется на Postgres сменой `DATABASE_URL`.
- **Frontend** — React + Telegram WebApp SDK (папка `frontend/`, добавим на Фазе 1).
- **Деплой** — Docker Compose: `app` (FastAPI) + `nginx` (статика + TLS-прокси). На слабом VPS можно перейти на bare systemd.

## Безопасность
- Вход в мини-апп — по Telegram `initData` (HMAC-подпись токеном бота), проверяется на сервере (`app/security.py`).
- **Вайтлист на один `OWNER_TG_ID`** — бот и API отвечают только владельцу.
- Webhook защищён `WEBHOOK_SECRET` (заголовок Telegram).
- Секреты — в `.env` (в `.gitignore`), не в коде. Файл БД и токены ФНС — в `backend/data/` (бэкапить эту папку).
- Бэкап БД — зашифрованный, в приватный Telegram-канал.
- Все вызовы LLM/ФНС — только на сервере; ключи наружу не уходят.

## Структура
```
backend/
  app/
    main.py            # FastAPI + webhook
    config.py          # настройки из .env
    db.py              # engine + сессия (SQLite WAL)
    models.py          # схема БД (SPEC §3)
    security.py        # проверка Telegram initData + вайтлист
    bot.py             # aiogram-хендлеры
    seed_categories.py # заливка categories.json в БД
    services/
      qr.py            # распознавание QR с фото
      fns.py           # клиент API ФНС (lkdr.nalog.ru)
  requirements.txt
  .env.example
  data/                # SQLite + tokens.json (gitignored)
deploy/                # Dockerfile, docker-compose.yml, nginx.conf
categories.json        # дерево категорий пользователя
SPEC.md                # спецификация
```

## Локальный запуск (dev)
```bash
cd backend
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # заполни BOT_TOKEN, OWNER_TG_ID, WEBHOOK_SECRET
python -m app.seed_categories ../categories.json
uvicorn app.main:app --reload
# проверка: GET http://localhost:8000/api/health
```

## Деплой на VPS
1. Установить Docker + docker-compose, склонировать репо.
2. `cp backend/.env.example backend/.env` и заполнить (BOT_TOKEN, OWNER_TG_ID, WEBHOOK_SECRET, PUBLIC_URL=https://домен, ключи LLM, бэкап).
3. Прописать домен в `deploy/nginx.conf`, получить сертификат (`certbot`), раскомментировать блок 443.
4. `cd deploy && docker compose up -d --build`.
5. Залить категории: `docker compose exec app python -m app.seed_categories`.
6. Webhook Telegram выставляется автоматически при старте (по `PUBLIC_URL`).

## Что нужно от тебя для запуска
- Домен (привязать к VPS).
- Токен бота от @BotFather + твой `OWNER_TG_ID` (узнать у @userinfobot).
- Доступ/характеристики VPS (ОС, RAM) — чтобы выбрать Docker vs bare.
- ID приватного канала для бэкапов + ключи OpenRouter (можно позже).
