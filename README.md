# PrintBot

PrintBot is a Telegram bot that lets you send documents and photos directly to a connected printer. It integrates with the CUPS (Common Unix Printing System) and supports deployment as a systemd service or Docker container.

---

## Features

- 📄 Print photos and documents sent via Telegram
- 🔒 Access control via configurable allowed chat IDs
- 🖨️ CUPS integration — uses the `lp` command under the hood
- 🟢 `/status` command to check live printer availability
- 📋 Command menu registered directly in Telegram
- 🐳 Docker and systemd deployment options
- ✅ No shell injection — all commands use argument lists

---

## Requirements

- Python 3.12+
- CUPS printing system with a configured printer
- A Telegram Bot API token — create one via [@BotFather](https://t.me/BotFather)

---

## Quick Start

```bash
git clone https://github.com/zr0aces/printbot.git
cd printbot
cp .env.example .env   # fill in your TOKEN and ALLOWED_CHAT_IDS
pip install -r requirements.txt
python bot.py
```

---

## Bot Commands

| Command | Description | Access |
|---------|-------------|--------|
| `/start` | Show the welcome message | All users |
| `/help` | Show available commands | All users |
| `/status` | Check printer availability via CUPS | All users |
| `/clean` | Delete cached downloaded files | Allowed users only |
| _(send a file/photo)_ | Print the file | Allowed users only |

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TOKEN` | ✅ Yes | Telegram Bot API token from [@BotFather](https://t.me/BotFather) |
| `ALLOWED_CHAT_IDS` | ❌ No | Comma-separated numeric Telegram chat IDs permitted to print/clean. Chat IDs are permanent and cannot be changed or spoofed — unlike usernames. **If unset, all Telegram users can send files to your printer** — set this in production. Find your ID via [@userinfobot](https://t.me/userinfobot). |

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

---

## Installation Options

### Option 1: systemd Service (Raspberry Pi / Linux)

#### Prerequisites

```bash
sudo apt-get install hplip cups python3 python3-pip
```

Configure your printer via the CUPS web interface at `http://localhost:631`.

#### Steps

1. Clone the repository:
   ```bash
   git clone https://github.com/zr0aces/printbot.git /home/pi/printbot
   cd /home/pi/printbot
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment variables:
   ```bash
   cp .env.example .env
   # Edit .env with your TOKEN and ALLOWED_CHAT_IDS
   ```

4. Install and enable the service:
   ```bash
   sudo cp printbot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable printbot.service
   sudo systemctl start printbot.service
   ```

5. Check status:
   ```bash
   sudo systemctl status printbot.service
   ```

---

### Option 2: Docker Compose (Recommended for Docker users)

#### Prerequisites

```bash
sudo apt-get install docker.io docker-compose-plugin
```

Ensure your printer is configured with CUPS on the host.

#### Steps

1. Clone and configure:
   ```bash
   git clone https://github.com/zr0aces/printbot.git
   cd printbot
   cp .env.example .env
   # Edit .env with your TOKEN and ALLOWED_CHAT_IDS
   ```

2. Build and start:
   ```bash
   docker compose up -d --build
   ```

3. View logs:
   ```bash
   docker compose logs -f
   ```

#### Manual Docker Run

```bash
docker build -t printbot .
docker run -d --name printbot \
  --device=/dev/usb/lp0 \
  -v /var/run/cups/cups.sock:/var/run/cups/cups.sock \
  --env-file .env \
  --restart always \
  printbot
```

---

## Notes

- The Docker image includes `cups-client` so the `lp` command is available inside the container.
- Files are printed with `-o fit-to-page -o media=A4` options.
- Downloaded files are cached in the `data/` directory and can be cleared with `/clean`.
- The bot registers its command list with Telegram on startup, so the `/` menu is always up to date.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Printer not detected | Check CUPS at `http://localhost:631`; ensure printer is added and enabled |
| Bot not responding | Verify your `TOKEN` is correct and the bot is running |
| `/status` shows no printers | Run `lpstat -p` on the host to confirm CUPS sees your printer |
| Permission issues | Ensure the running user has access to CUPS (`lpadmin` group) |
| Docker can't reach printer | Confirm `/dev/usb/lp0` and `/var/run/cups/cups.sock` are forwarded |

---

Contributions and issues are welcome!

