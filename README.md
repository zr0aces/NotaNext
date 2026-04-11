# PrintBot

[![Release](https://img.shields.io/github/v/release/zr0aces/PrintBot)](https://github.com/zr0aces/PrintBot/releases)
[![Docker](https://img.shields.io/badge/ghcr.io-zr0aces%2Fprintbot-blue)](https://github.com/zr0aces/PrintBot/pkgs/container/printbot)

## Overview

PrintBot is a Telegram bot that sends photos and documents straight to a CUPS-connected printer. Run it on a Raspberry Pi (or any Linux box) as a systemd service or Docker container, then print anything from your phone by just sending it to the bot.

**Key features**

- 📄 Print photos and documents sent via Telegram
- ⚙️ Print options — send `bw`, `2x`, `3x`, `4x` before a file to customise the print
- 🔒 Access control via numeric chat IDs (`ALLOWED_CHAT_IDS`)
- 🖨️ CUPS integration — uses the `lp` command under the hood
- 🟢 `/status` command to check live printer availability
- 📭 `/jobs` and `/cancel` commands for queue management
- 🏠 Optional Home Assistant webhook after each print
- 🐳 Docker and systemd deployment options
- ✅ No shell injection — all commands use argument lists

---

## How to Use

### 1. Prerequisites

- A printer configured in CUPS (`http://localhost:631`)
- A Telegram Bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram chat ID — send a message to [@userinfobot](https://t.me/userinfobot) to find it

For **systemd** deployment, you also need:

```bash
sudo apt-get install hplip cups python3 python3-pip
```

For **Docker** deployment:

```bash
sudo apt-get install docker.io docker-compose-plugin
```

---

### 2. Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|----------|----------|-------------|
| `TOKEN` | ✅ Yes | Telegram Bot API token from [@BotFather](https://t.me/BotFather) |
| `ALLOWED_CHAT_IDS` | ❌ No | Comma-separated numeric chat IDs permitted to print/clean. **If unset, anyone can print.** Find your ID via [@userinfobot](https://t.me/userinfobot). |
| `HA_URL` | ❌ No | Home Assistant base URL (e.g. `http://homeassistant:8123`). Required together with `HA_TOKEN` for webhook integration. |
| `HA_TOKEN` | ❌ No | Home Assistant long-lived access token. Required together with `HA_URL` for webhook integration. |

---

### 3. Run

#### Option A — Docker Compose (recommended)

```bash
git clone https://github.com/zr0aces/printbot.git
cd printbot
cp .env.example .env   # fill in TOKEN and ALLOWED_CHAT_IDS
docker compose up -d --build
docker compose logs -f
```

#### Option B — systemd Service (Raspberry Pi / Linux)

```bash
git clone https://github.com/zr0aces/printbot.git /home/pi/printbot
cd /home/pi/printbot
pip install -r requirements.txt
cp .env.example .env   # fill in TOKEN and ALLOWED_CHAT_IDS

sudo cp printbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now printbot.service
sudo systemctl status printbot.service
```

#### Option C — Run directly

```bash
git clone https://github.com/zr0aces/printbot.git
cd printbot
pip install -r requirements.txt
cp .env.example .env   # fill in TOKEN and ALLOWED_CHAT_IDS
python bot.py
```

---

### 4. Send a File to Print

Once the bot is running, open it in Telegram and:

1. Send any **photo** or **document** — the bot forwards it to the printer automatically.
2. Use the commands below for status and maintenance.

#### Bot Commands

| Command | Description | Who can use |
|---------|-------------|-------------|
| `/start` | Show the welcome message | Everyone |
| `/help` | List available commands | Everyone |
| `/status` | Check printer availability via CUPS | Everyone |
| `/jobs` | Show the current print queue | Allowed chat IDs only |
| `/cancel` | Cancel all pending print jobs | Allowed chat IDs only |
| `/clean` | Delete cached downloaded files | Allowed chat IDs only |

#### Print Options

Before sending a file, you can text the bot with options for the next print:

| Option | Effect |
|--------|--------|
| `bw` or `gray` | Print in black & white |
| `2x`, `3x`, `4x` | Print multiple copies |
| `bw 2x` | Combine options |

Options apply to the **next** file only, then reset to defaults (colour, 1 copy).

---

## Releases

Docker images are published automatically to [GitHub Container Registry](https://github.com/zr0aces/PrintBot/pkgs/container/printbot) when a version tag is pushed.

Pull the latest release:

```bash
docker pull ghcr.io/zr0aces/printbot:latest
```

Or pin to a specific version:

```bash
docker pull ghcr.io/zr0aces/printbot:1.0.0
```

See [CHANGELOG.md](CHANGELOG.md) for release notes.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Printer not detected | Check CUPS at `http://localhost:631`; ensure the printer is added and enabled |
| Bot not responding | Verify `TOKEN` in `.env` is correct and the bot process is running |
| `/status` shows no printers | Run `lpstat -p` on the host to confirm CUPS sees your printer |
| Permission issues | Ensure the running user is in the `lpadmin` group |
| Docker can't reach printer | Confirm `/dev/usb/lp0` and `/var/run/cups/cups.sock` are forwarded in `docker-compose.yml` |

---

Contributions and issues are welcome!

