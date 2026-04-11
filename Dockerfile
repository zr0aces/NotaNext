FROM python:3.12-slim

ARG VERSION=dev

LABEL org.opencontainers.image.title="PrintBot" \
      org.opencontainers.image.description="Telegram bot that sends files to a CUPS printer" \
      org.opencontainers.image.source="https://github.com/zr0aces/PrintBot" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update && \
    apt-get install -y --no-install-recommends cups-client && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app
CMD ["python", "bot.py"]