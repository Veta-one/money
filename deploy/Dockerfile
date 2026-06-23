FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Europe/Moscow

# libzbar0 — для pyzbar (QR); tzdata — чтобы TZ=Europe/Moscow реально применялась
# (в slim-образе без tzdata date.today()/datetime.now() всегда UTC, и снапшот
# капитала создавался на сутки позже московской даты).
RUN apt-get update && apt-get install -y --no-install-recommends libzbar0 tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ .
COPY categories.json ./categories.json
COPY frontend/ ./frontend/

RUN useradd -m appuser && mkdir -p /app/data && chown -R appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
