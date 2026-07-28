FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY repost/ ./repost/
COPY scripts/ ./scripts/

# База и sources.txt монтируются как volume — переживают пересборку образа
VOLUME ["/data"]
ENV DB_PATH=/data/repost.db

CMD ["python", "-m", "repost.bot"]
