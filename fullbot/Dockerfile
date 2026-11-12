# Merged bot (mts-bot + checketop) — Render Web Service
FROM python:3.11-slim

WORKDIR /app

# Java headless нужен JZComp (deflate законов банков) — в slim его нет
RUN apt-get update && apt-get install -y --no-install-recommends \
    default-jre-headless fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

ENV PORT=10000 \
    JAVA_BIN=/usr/bin/java \
    JZCOMP_DIR=/app \
    TEMP=/tmp \
    PYTHONUNBUFFERED=1

CMD ["python3", "run_render.py"]
