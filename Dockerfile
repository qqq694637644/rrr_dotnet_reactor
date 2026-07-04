FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUTH_ENV=production \
    AUTH_COOKIE_SECURE=true \
    AUTH_DB_PATH=/data/authorization.db

WORKDIR /app

COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY app ./app
COPY README.md ./README.md

RUN useradd --system --uid 10001 --create-home --home-dir /home/appuser appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser
VOLUME ["/data"]

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
