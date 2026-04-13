FROM ubuntu:22.04

ARG VERSION=dev

LABEL org.opencontainers.image.title="PrintBot" \
      org.opencontainers.image.description="Telegram bot that sends files to a CUPS printer" \
      org.opencontainers.image.source="https://github.com/zr0aces/PrintBot" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"

# Avoid interactive prompts during build
ENV DEBIAN_FRONTEND=noninteractive

# Install Python 3, pip, and CUPS client 2.4.1
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    cups-client \
    ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . /app

# Use python3 explicitly for Ubuntu
CMD ["python3", "bot.py"]