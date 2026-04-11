import os
import subprocess
import time
import logging

from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

logger = logging.getLogger("bot")

DATA_DIR = "data"


def get_allowed_usernames():
    """Parse allowed usernames from environment variable (comma or space separated)."""
    raw = os.getenv("ALLOWED_USERNAMES", "")
    # Support both comma-separated and space-separated formats
    return [u.strip() for u in raw.replace(",", " ").split() if u.strip()]


async def start(update, context):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="I'm Printer, send me a photo or document to print!",
    )


async def clean(update, context):
    if os.path.exists(DATA_DIR):
        for filename in os.listdir(DATA_DIR):
            filepath = os.path.join(DATA_DIR, filename)
            try:
                if os.path.isfile(filepath):
                    os.remove(filepath)
            except OSError as e:
                logger.error(f"Error removing {filepath}: {e}")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Cleaning up old downloaded files from data folder",
    )


async def print_msg(update, context):
    logger.info("Received message type: %s", type(update))
    logger.info("Message: %s", update.effective_message)

    file = None
    if update.effective_message.photo:
        photo = max(update.effective_message.photo, key=lambda x: x.file_size)
        file = await photo.get_file()
    elif update.effective_message.document:
        file = await update.effective_message.document.get_file()

    if file is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Could not find a printable file in your message.",
        )
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    file_path = os.path.join(DATA_DIR, str(int(time.time())))
    await file.download_to_drive(file_path)
    logger.info("File saved at %s", file_path)

    try:
        print_file(file_path)
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text="Printing..."
        )
    except Exception as e:
        logger.error("Print failed: %s", e)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Print failed. Please check the printer and try again.",
        )


def print_file(file_path):
    """Send a file to the printer using lp."""
    cmd = ["/usr/bin/lp", "-o", "fit-to-page", "-o", "media=A4", file_path]
    logger.info("Printing %s", file_path)
    logger.debug("Command: %s", cmd)
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logger.error("lp stderr: %s", result.stderr)
        raise RuntimeError("Print command failed")
    logger.info("lp stdout: %s", result.stdout)


def main():
    token = os.getenv("TOKEN")
    if not token:
        logger.error("TOKEN environment variable is not set")
        raise SystemExit("TOKEN environment variable is required")

    allowed_usernames = get_allowed_usernames()

    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", start))

    if allowed_usernames:
        username_filter = filters.Chat(username=allowed_usernames)
    else:
        username_filter = filters.ALL

    application.add_handler(
        CommandHandler("clean", clean, filters=username_filter)
    )

    application.add_handler(
        MessageHandler(
            username_filter
            & (filters.PHOTO | filters.Document.ALL)
            & (~filters.COMMAND),
            print_msg,
        )
    )

    logger.info("Bot starting with allowed users: %s", allowed_usernames or "ALL")
    application.run_polling()


if __name__ == "__main__":
    main()
