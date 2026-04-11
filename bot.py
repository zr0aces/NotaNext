import os
import shutil
import subprocess
import time
import logging

from telegram import BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

logger = logging.getLogger("printbot")

DATA_DIR = "data"

HELP_TEXT = (
    "📠 *PrintBot Commands*\n\n"
    "/start — Show the welcome message\n"
    "/help — Show this help message\n"
    "/status — Check printer availability\n"
    "/clean — Delete cached files (allowed users only)\n\n"
    "Send a *photo* or *document* to print it."
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
        result = subprocess.run(
            [lpstat, "-p"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            msg = f"🟢 Printer is available:\n```\n{result.stdout.strip()}\n```"
        else:
            msg = "🔴 No printers are currently available. Check CUPS configuration."
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

    try:
        print_file(file_path)
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text="✅ Sent to printer!"
        )
    except Exception as e:
        logger.error("Print failed: %s", e)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Print failed. Please check the printer and try again.",
        )


def print_file(file_path):
    """Send a file to the printer using lp."""
    lp = shutil.which("lp")
    if not lp:
        raise RuntimeError("lp command not found — is cups-client installed?")
    cmd = [lp, "-o", "fit-to-page", "-o", "media=A4", file_path]
    logger.info("Printing %s", file_path)
    logger.debug("Command: %s", cmd)
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logger.error("lp stderr: %s", result.stderr)
        raise RuntimeError("Print command failed")
    logger.info("lp stdout: %s", result.stdout)


async def post_init(application):
    """Register bot commands in the Telegram menu after startup."""
    await application.bot.set_my_commands([
        BotCommand("start", "Show the welcome message"),
        BotCommand("help", "Show available commands"),
        BotCommand("status", "Check printer availability"),
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

    logger.info("PrintBot starting with allowed chat IDs: %s", allowed_chat_ids or "ALL")
    application.run_polling()


if __name__ == "__main__":
    main()
