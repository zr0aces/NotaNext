# PrintBot

[![Release](https://img.shields.io/github/v/release/zr0aces/PrintBot)](https://github.com/zr0aces/PrintBot/releases)
[![Docker Release](https://github.com/zr0aces/PrintBot/actions/workflows/docker-release.yml/badge.svg)](https://github.com/zr0aces/PrintBot/actions/workflows/docker-release.yml)
[![Docker](https://img.shields.io/badge/ghcr.io-zr0aces%2Fprintbot-blue)](https://github.com/zr0aces/PrintBot/pkgs/container/printbot)
[![Python](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org/)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-22.7-blue)](https://python-telegram-bot.org/)

## Overview

PrintBot is a Telegram bot that sends photos and documents straight to a CUPS-connected printer. Run it on a Raspberry Pi (or any Linux box) as a systemd service or a Docker container, then print anything from your phone by just sending it to the bot.

**Key features**

- 📄 Print photos and documents sent via Telegram
- ⚙️ Print options — send `bw`, `2x`, `3x`, `4x` before a file to customise the print
- 🔒 Access control via numeric chat IDs (`ALLOWED_CHAT_IDS`)
- 🖨️ CUPS integration — uses `lp` with explicit `-h <server>` and `-d <printer>` flags
- 🌐 Remote CUPS server support — connects over TCP port 631
- 🟢 `/status` command to check live printer availability
- 📭 `/jobs` and `/cancel` commands for queue management
- 🏠 Optional Home Assistant webhook after each print
- 🐳 Docker (multi-arch: amd64 & arm64) and systemd deployment options
- 🪵 Configurable log level via `LOG_LEVEL` env var
- ✅ No shell injection — all commands use argument lists

---

## How to Use

### 1. Prerequisites

- A printer configured in CUPS (`http://<cups-host>:631`)
- A Telegram Bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram chat ID — send a message to [@userinfobot](https://t.me/userinfobot) to find it

For **systemd** deployment, you also need:

```bash
sudo apt-get install cups-client python3 python3-pip
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
chmod 600 .env   # restrict read access — .env contains your Telegram token
```

| Variable | Required | Description |
|----------|----------|-------------|
| `TOKEN` | ✅ Yes | Telegram Bot API token from [@BotFather](https://t.me/BotFather) |
| `CUPS_SERVER` | ✅ Yes | CUPS server hostname or IP (e.g. `cups` or `192.168.1.100`). Default: `cups`. |
| `PRINTER_NAME` | ✅ Yes | Printer name as registered in CUPS (e.g. `Canon_LBP7110Cw`). Run `lpstat -v` to list available printers. |
| `ALLOWED_CHAT_IDS` | ❌ No | Comma-separated numeric chat IDs permitted to print/clean. **If unset, anyone can print.** Find your ID via [@userinfobot](https://t.me/userinfobot). |
| `LOG_LEVEL` | ❌ No | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. Default: `INFO`. |
| `HA_URL` | ❌ No | Home Assistant base URL (e.g. `http://homeassistant:8123`). Required together with `HA_TOKEN`. |
| `HA_TOKEN` | ❌ No | Home Assistant long-lived access token. Required together with `HA_URL`. |
| `TZ` | ❌ No | Timezone for container logs (e.g. `Asia/Bangkok`). Default: UTC. |

---

### 3. Run

#### Option A — Docker Compose (recommended)

```bash
git clone https://github.com/zr0aces/printbot.git
cd printbot
cp .env.example .env   # fill in TOKEN, CUPS_SERVER, PRINTER_NAME
docker compose up -d
docker compose logs -f
```

The `docker-compose.yml` pulls directly from GitHub Container Registry — no local build required.

To build locally instead:

```bash
docker compose up -d --build
```

#### Option B — systemd Service (Raspberry Pi / Linux)

```bash
git clone https://github.com/zr0aces/printbot.git /home/pi/printbot
cd /home/pi/printbot
pip install -r requirements.txt
cp .env.example .env   # fill in TOKEN, CUPS_SERVER, PRINTER_NAME

sudo cp printbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now printbot.service
sudo systemctl status printbot.service
```

> **Note:** The `printbot.service` file assumes the user `pi` and path `/home/pi/printbot`. Adjust `User=` and `WorkingDirectory=` as needed.

#### Option C — Run directly

```bash
git clone https://github.com/zr0aces/printbot.git
cd printbot
pip install -r requirements.txt
cp .env.example .env   # fill in TOKEN, CUPS_SERVER, PRINTER_NAME
python3 bot.py
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

Before sending a file, text the bot with one or more options for the **next** print:

| Option | Effect |
|--------|--------|
| `bw` or `gray` | Print in black & white |
| `2x`, `3x`, `4x` | Print multiple copies |
| `bw 2x` | Combine: B&W + 2 copies |

Options apply to the **next** file only, then reset to defaults (colour, 1 copy). They expire after 10 minutes if no file is sent.

---

## Releases

Docker images are published automatically to [GitHub Container Registry](https://github.com/zr0aces/PrintBot/pkgs/container/printbot) when a version tag is pushed. Multi-arch images are built for **linux/amd64** and **linux/arm64** (Raspberry Pi).

Pull the latest release:

```bash
docker pull ghcr.io/zr0aces/printbot:latest
```

Pin to a specific version:

```bash
docker pull ghcr.io/zr0aces/printbot:1.0.7
```

Track minor-version patch updates:

```bash
docker pull ghcr.io/zr0aces/printbot:1.0
```

See [CHANGELOG.md](CHANGELOG.md) for release notes.

---

## Tech Stack

| Component | Version |
|-----------|---------|
| Python | 3.12 |
| [python-telegram-bot](https://python-telegram-bot.org/) | 22.7 |
| [httpx](https://www.python-httpx.org/) | transitive dep (async HTTP) |
| CUPS client (`lp` / `lpstat` / `cancel`) | System package |
| Docker base image | `ubuntu:22.04` |

> **Supply-chain hardening:** To pin `requirements.txt` with SHA-256 hashes, run
> `pip install pip-tools && pip-compile --generate-hashes requirements.txt`
> and commit the resulting `requirements.txt` lockfile.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `No default destination` error | Ensure `PRINTER_NAME` is set and matches a printer registered on the CUPS server (`lpstat -v -h <server>`). |
| Printer not detected | Check CUPS at `http://<cups-host>:631`; ensure the printer is added and enabled. |
| Bot not responding | Verify `TOKEN` in `.env` is correct and the bot process is running (`docker compose logs printbot`). |
| `/status` shows no printers | Run `lpstat -p -h <cups-host>` on the host to confirm CUPS sees your printer. |
| Connection refused on port 631 | Make sure the CUPS container/service exposes port 631 and `CUPS_SERVER` points to the correct host. |
| Permission issues (systemd) | Ensure the running user is in the `lpadmin` group (`sudo usermod -aG lpadmin $USER`). |
| Canon driver not found | Verify the PPD file `CNRCUPSLBP7110CZNK.ppd` is installed in `/usr/share/cups/model/` on the CUPS server. |

---

Contributions and issues are welcome!
