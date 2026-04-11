# PrinterMasterBot

PrinterMasterBot is a Telegram bot that allows users to send documents or photos directly to a connected printer. It integrates with the CUPS (Common Unix Printing System) to manage print jobs and supports deployment as a system service or Docker container.

---

## Features

- Accepts files or photos via Telegram and sends them to a connected printer.
- Configurable via environment variables for access control and bot token.
- Supports USB-connected printers with CUPS integration.
- Flexible deployment: run as a system service or as a Docker container.
- Secure: no shell injection, safe file handling, and proper error reporting.

---

## Requirements

- Python 3.12+
- CUPS printing system with a configured printer
- A Telegram Bot API token (create one via [@BotFather](https://t.me/BotFather))

---

## Installation Options

### Option 1: Run as a System Service

#### Prerequisites

1. **Install Required Software**:
   ```bash
   sudo apt-get install hplip cups python3 python3-pip
   ```
2. **Configure Your Printer**:
   - Open the CUPS web interface (`http://localhost:631`).
   - Add your printer and enable sharing for network printing.

#### Steps

1. Clone the repository and navigate to the project directory:
   ```bash
   git clone https://github.com/your-repo/printermasterbot.git
   cd printermasterbot
   ```

2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment variables:
   - Copy `.env.example` to `.env` and fill in your `TOKEN` and `ALLOWED_USERNAMES`.
   - Or edit the service file `printermasterbot.service` with your values.

4. Install and enable the service:
   ```bash
   sudo cp printermasterbot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable printermasterbot.service
   sudo systemctl start printermasterbot.service
   ```

---

### Option 2: Run as a Docker Container

#### Prerequisites

- **Install Docker**:
  ```bash
  sudo apt-get install docker.io docker-compose-plugin
  ```

- Ensure your printer is configured with CUPS on the host.

#### Using Docker Compose (Recommended)

1. Copy `.env.example` to `.env` and fill in your values:
   ```bash
   cp .env.example .env
   ```

2. Build and start the container:
   ```bash
   docker compose up -d --build
   ```

#### Manual Docker Build and Run

1. **Build the Docker Image**:
   ```bash
   docker build -t printermasterbot .
   ```

2. **Run the Docker Container**:
   ```bash
   docker run -d --name printermasterbot \
     --device=/dev/usb/lp0 \
     -v /var/run/cups/cups.sock:/var/run/cups/cups.sock \
     -e TOKEN=your-telegram-bot-token \
     -e ALLOWED_USERNAMES=user1,user2,user3 \
     --restart always \
     printermasterbot
   ```

---

## Usage

1. Find the bot on Telegram by searching for `@your_bot_username`.
2. Start the bot with the `/start` command.
3. Send a document or photo to the bot, and it will send it to the printer.
4. Use the `/clean` command (restricted to allowed users) to clear old files.

---

## Environment Variables

- **`TOKEN`**: (Required) Your Telegram Bot API token.
  - Example: `123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ`

- **`ALLOWED_USERNAMES`**: (Optional) A comma-separated list of Telegram usernames allowed to use the bot. If unset, all users can interact with the bot.
  - Example: `user1,user2,user3`

---

## Notes

- Ensure your printer is powered on and connected before starting the bot.
- For Docker deployments, make sure to forward USB devices and CUPS sockets to the container.
- The bot uses the `lp` command to send files to the printer with options like `fit-to-page` and `A4` media.
- The Docker image includes `cups-client` for the `lp` command.

---

## Troubleshooting

- **Printer not detected**: Check CUPS configuration and ensure the printer is added and shared.
- **Bot not responding**: Verify your Telegram Bot API token and network connection.
- **Permission issues**: Ensure the user running the bot has access to the printer and CUPS.
- **Print failures**: Check the bot logs for `lp` command errors. Ensure CUPS is running and the printer is available.

Contributions and issues are welcome to improve PrinterMasterBot!
