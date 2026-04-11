# Changelog

All notable changes to this project will be documented in this file.

## [1.0.0] – 2026-04-11

### Added
- Print options via message prefix (`bw`, `gray`, `2x`–`4x`) — set per chat, applied to next file, then reset
- `/jobs` command — shows the current CUPS print queue via `lpstat -o`
- `/cancel` command — cancels all pending jobs via `cancel -a`
- Home Assistant webhook integration — fires `printbot_job_sent` event after each successful print (`HA_URL` + `HA_TOKEN` env vars)
- `VERSION` constant in `bot.py`, logged at startup
- OCI image labels in `Dockerfile` (`org.opencontainers.image.*`)
- `.dockerignore` to minimise image build context
- GitHub Actions workflow to build and push multi-arch Docker images on version tags
- `CHANGELOG.md`

### Improved
- Print errors now include CUPS stderr output in the Telegram reply
- Auto-cleanup: downloaded file is removed immediately after a successful print
- `HELP_TEXT` updated to document all commands and print options
- `docker-compose.yml` uses a versioned image name (`ghcr.io/zr0aces/printbot:1.0.0`)

### Removed
- `Pipfile` (redundant alongside `requirements.txt`)
- `.vscode/settings.json` (machine-specific local path)

### Initial feature set
- Telegram bot using python-telegram-bot v22.7
- CUPS integration via `lp` subprocess
- Access control via `ALLOWED_CHAT_IDS` (numeric chat IDs)
- `/start`, `/help`, `/status`, `/clean` commands
- Docker Compose and systemd deployment options
