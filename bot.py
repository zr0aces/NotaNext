import json
import os
import shutil
import subprocess
import time
import logging
import urllib.request

from telegram import BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

logger = logging.getLogger("printbot")

VERSION = "1.0.0"
DATA_DIR = "data"

# Per-chat print options: {chat_id: {"color": bool, "copies": int}}
print_options = {}

# Maximum characters of stderr to include in error replies
MAX_STDERR_LENGTH = 120

def get_cups_args():
    """Build common arguments for CUPS commands based on environment variables."""
    args = []
    server = os.getenv("CUPS_SERVER")
    if server:
        args += ["-h", server]
    return args

def get_printer_name():
    """Get the target printer name from environment variables."""
    return os.getenv("PRINTER_NAME")

HELP_TEXT = (
    "📠 *PrintBot Commands*\n\n"
    "/start — Show the welcome message\n"
    "/help — Show this help message\n"
    "/status — Check printer availability\n"
    "/jobs — Show the print queue\n"
    "/cancel — Cancel all print jobs\n"
    "/clean — Delete cached files (allowed users only)\n\n"
    "Send a *photo* or *document* to print it.\n\n"
    "*Print options* — send before your file:\n"
    "  `bw` or `gray` — black & white\n"
    "  `2x`, `3x`, `4x` — multiple copies\n"
    "  `bw 2x` — combine options"
)


def get_allowed_chat_ids():
    """Parse allowed chat IDs from environment variable (comma or space separated).

    Returns a list of integer chat IDs. Chat IDs are permanent numeric identifiers
    assigned by Telegram and cannot be changed or spoofed, making them more reliable
    than usernames for access control.
    """
    raw = os.getenv("ALLOWED_CHAT_IDS", "")
    ids = []
    for part in raw.replace(",", " ").split():
        part = part.strip()
        if part:
            try:
                ids.append(int(part))
            except ValueError:
                logger.warning("Ignoring non-integer value in ALLOWED_CHAT_IDS: %r", part)
    return ids


async def start(update, context):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "👋 Welcome to *PrintBot*!\n\n"
            "Send me a photo or document and I'll print it for you.\n\n"
            "Use /help to see all available commands."
        ),
        parse_mode="Markdown",
    )


async def help_command(update, context):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=HELP_TEXT,
        parse_mode="Markdown",
    )


async def status(update, context):
    """Report whether the printer is reachable via CUPS."""
    lpstat = shutil.which("lpstat")
    if not lpstat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ CUPS client tools (`lpstat`) not found on this system.",
        )
        return

    try:
        cmd = [lpstat] + get_cups_args() + ["-p"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            if result.stdout.strip():
                msg = f"🟢 Printer is available:\n```\n{result.stdout.strip()}\n```"
            else:
                msg = "🟡 No printers are currently registered on the server."
        else:
            stderr = result.stderr.strip()[:MAX_STDERR_LENGTH]
            msg = f"🔴 Could not reach printer server:\n`{stderr or 'Unknown error'}`"
    except subprocess.TimeoutExpired:
        msg = "⚠️ Printer status check timed out."

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=msg,
        parse_mode="Markdown",
    )


async def clean(update, context):
    removed = 0
    if os.path.exists(DATA_DIR):
        for filename in os.listdir(DATA_DIR):
            filepath = os.path.join(DATA_DIR, filename)
            try:
                if os.path.isfile(filepath):
                    os.remove(filepath)
                    removed += 1
            except OSError as e:
                logger.error("Error removing %s: %s", filepath, e)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"🗑️ Cleaned up {removed} cached file(s) from the data folder.",
    )


async def jobs_command(update, context):
    """Show the current CUPS print queue."""
    lpstat = shutil.which("lpstat")
    if not lpstat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ CUPS client tools (`lpstat`) not found on this system.",
        )
        return

    try:
        cmd = [lpstat] + get_cups_args() + ["-o"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            if result.stdout.strip():
                msg = f"🖨️ Print queue:\n```\n{result.stdout.strip()}\n```"
            else:
                msg = "📭 No jobs in queue"
        else:
            stderr = result.stderr.strip()[:MAX_STDERR_LENGTH]
            msg = f"⚠️ Could not fetch queue: `{stderr or 'Unknown error'}`"
    except subprocess.TimeoutExpired:
        msg = "⚠️ Print queue check timed out."

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=msg,
        parse_mode="Markdown",
    )


async def cancel_command(update, context):
    """Cancel all pending print jobs."""
    cancel_bin = shutil.which("cancel")
    if not cancel_bin:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ CUPS client tools (`cancel`) not found on this system.",
        )
        return

    try:
        cmd = [cancel_bin] + get_cups_args() + ["-a"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            msg = "🗑️ All print jobs cancelled"
        else:
            stderr = result.stderr.strip()[:MAX_STDERR_LENGTH]
            msg = f"⚠️ Could not cancel jobs: {stderr}"
    except subprocess.TimeoutExpired:
        msg = "⚠️ Cancel command timed out."

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=msg,
    )


COPY_OPTIONS = {"2x": 2, "3x": 3, "4x": 4}


async def set_print_options(update, context):
    """Parse print option keywords from a text message."""
    chat_id = update.effective_chat.id
    text = update.effective_message.text.strip().lower()

    color = True
    copies = 1
    valid = True

    tokens = text.split()
    for token in tokens:
        if token in ("bw", "gray"):
            color = False
        elif token in COPY_OPTIONS:
            copies = COPY_OPTIONS[token]
        else:
            valid = False

    if not valid or not tokens:
        await context.bot.send_message(
            chat_id=chat_id,
            text="❓ Unknown option. Use: `bw`, `2x`, `3x`, `4x`, `bw 2x`",
            parse_mode="Markdown",
        )
        return

    print_options[chat_id] = {"color": color, "copies": copies}

    parts = []
    parts.append("B&W" if not color else "Color")
    parts.append(f"{copies} copies" if copies > 1 else "1 copy")

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⚙️ Next print: {', '.join(parts)}",
    )


async def print_msg(update, context):
    logger.info("Received message from %s", update.effective_user.username)

    file = None
    if update.effective_message.photo:
        photo = max(update.effective_message.photo, key=lambda x: x.file_size)
        file = await photo.get_file()
    elif update.effective_message.document:
        file = await update.effective_message.document.get_file()

    if file is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Could not find a printable file in your message.",
        )
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    file_path = os.path.join(DATA_DIR, str(int(time.time())))
    await file.download_to_drive(file_path)
    logger.info("File saved at %s", file_path)

    chat_id = update.effective_chat.id
    opts = print_options.pop(chat_id, {"color": True, "copies": 1})
    color = opts["color"]
    copies = opts["copies"]

    try:
        print_file(file_path, color=color, copies=copies)
        await context.bot.send_message(
            chat_id=chat_id, text="✅ Sent to printer!"
        )
        try:
            os.remove(file_path)
            logger.info("Cleaned up %s", file_path)
        except OSError as e:
            logger.warning("Could not remove %s: %s", file_path, e)
        notify_homeassistant(
            file_name=os.path.basename(file_path),
            chat_id=chat_id,
            copies=copies,
            color=color,
        )
    except Exception as e:
        logger.error("Print failed: %s", e)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Print failed: {e}",
        )


def print_file(file_path, color=True, copies=1):
    """Send a file to the printer using lp."""
    lp = shutil.which("lp")
    if not lp:
        raise RuntimeError("lp command not found — is cups-client installed?")
    cmd = [lp] + get_cups_args() + ["-o", "fit-to-page", "-o", "media=A4"]
    
    printer = get_printer_name()
    if printer:
        cmd += ["-d", printer]

    if not color:
        cmd += ["-o", "ColorModel=Gray"]
    if copies > 1:
        cmd += ["-n", str(copies)]
    cmd.append(file_path)
    logger.info("Printing %s", file_path)
    logger.debug("Command: %s", cmd)
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip()[:MAX_STDERR_LENGTH]
        logger.error("lp stderr: %s", result.stderr)
        raise RuntimeError(stderr or "Print command failed")
    logger.info("lp stdout: %s", result.stdout)


def notify_homeassistant(file_name, chat_id, copies, color):
    """Fire a Home Assistant event after a successful print (best-effort)."""
    ha_url = os.getenv("HA_URL")
    ha_token = os.getenv("HA_TOKEN")
    if not ha_url or not ha_token:
        return

    url = f"{ha_url}/api/events/printbot_job_sent"
    payload = json.dumps({
        "file_name": file_name,
        "chat_id": chat_id,
        "copies": copies,
        "color": color,
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=3).close()
        logger.info("Home Assistant notified: %s", url)
    except Exception as e:
        logger.warning("Home Assistant notification failed: %s", e)


async def post_init(application):
    """Register bot commands in the Telegram menu after startup."""
    await application.bot.set_my_commands([
        BotCommand("start", "Show the welcome message"),
        BotCommand("help", "Show available commands"),
        BotCommand("status", "Check printer availability"),
        BotCommand("jobs", "Show the print queue"),
        BotCommand("cancel", "Cancel all print jobs"),
        BotCommand("clean", "Delete cached files (allowed users only)"),
    ])


def main():
    token = os.getenv("TOKEN")
    if not token:
        logger.error("TOKEN environment variable is not set")
        raise SystemExit("TOKEN environment variable is required")

    allowed_chat_ids = get_allowed_chat_ids()

    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status))

    if allowed_chat_ids:
        chat_id_filter = filters.Chat(chat_id=allowed_chat_ids)
    else:
        logger.warning(
            "ALLOWED_CHAT_IDS is not set — all Telegram users can print. "
            "Set this variable to restrict access."
        )
        chat_id_filter = filters.ALL

    application.add_handler(
        CommandHandler("jobs", jobs_command, filters=chat_id_filter)
    )
    application.add_handler(
        CommandHandler("cancel", cancel_command, filters=chat_id_filter)
    )
    application.add_handler(
        CommandHandler("clean", clean, filters=chat_id_filter)
    )

    application.add_handler(
        MessageHandler(
            chat_id_filter
            & (filters.PHOTO | filters.Document.ALL)
            & (~filters.COMMAND),
            print_msg,
        )
    )

    application.add_handler(
        MessageHandler(
            chat_id_filter
            & filters.TEXT
            & (~filters.COMMAND),
            set_print_options,
        )
    )

    logger.info("PrintBot v%s starting with allowed chat IDs: %s", VERSION, allowed_chat_ids or "ALL")
    application.run_polling()


if __name__ == "__main__":
    main()
