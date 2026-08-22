FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MALLOC_ARENA_MAX=2 \
    MALLOC_TRIM_THRESHOLD_=65536

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.boot:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
