FROM ubuntu:22.04

ARG VERSION=dev

LABEL org.opencontainers.image.title="PimNext" \
      org.opencontainers.image.description="Telegram bot that sends files to a CUPS printer" \
      org.opencontainers.image.source="https://github.com/zr0aces/PimNext" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"

# Avoid interactive prompts during build
ENV DEBIAN_FRONTEND=noninteractive

# Install Python 3, pip, CUPS client, and healthcheck utilities
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    cups-client \
    ca-certificates \
    curl \
    procps && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . /app

# Setup entrypoint for CUPS configuration
RUN chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# Basic healthcheck: ensure the bot process is active
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD pgrep -f bot.py || exit 1

# Use python3 explicitly for Ubuntu
CMD ["python3", "bot.py"]