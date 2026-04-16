import asyncio
import os
import shutil
import time
import logging
import uuid

import httpx
from telegram import BotCommand, Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

# Validate and apply LOG_LEVEL — warn loudly if the value is unrecognised
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
numeric_level = getattr(logging, log_level, None)
if not isinstance(numeric_level, int):
    print(f"WARNING: Invalid LOG_LEVEL={log_level!r} — defaulting to INFO.")
    numeric_level = logging.INFO

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=numeric_level,
)

# Suppress noisy library loggers — only show WARNING and above from these
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

logger = logging.getLogger("pimnext")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "1.1.0"
DATA_DIR = "data"

# Maximum characters of stderr to include in error replies
MAX_STDERR_LENGTH = 300

# Maximum file size accepted for printing (20 MB — Telegram bot limit)
MAX_FILE_BYTES = 20 * 1024 * 1024

# Printable document extensions (lowercase). Photos are always accepted.
PRINTABLE_EXTENSIONS = {
    ".pdf", ".ps",
    ".jpg", ".jpeg", ".png", ".gif",
    ".txt",
    ".doc", ".docx", ".odt",
}

# Pre-sorted display string — reused in every unsupported-type error reply
PRINTABLE_EXTENSIONS_DISPLAY = ", ".join(sorted(PRINTABLE_EXTENSIONS))

# Per-chat print options: {chat_id: {"color": bool, "copies": int, "media": str, "number_up": int, "ts": float}}
# Entries expire after PRINT_OPTIONS_TTL seconds.
print_options: dict[int, dict] = {}
PRINT_OPTIONS_TTL = 1800  # 30 minutes

# Per-chat rate limiting — minimum seconds between accepted print jobs
PRINT_COOLDOWN = 10  # seconds
last_print_time: dict[int, float] = {}

# Per-chat half-mode queue: {chat_id: {"files": [path, ...], "ts": float}}
# Holds downloaded file paths waiting to be paired before printing.
half_queue: dict[int, dict] = {}
HALF_QUEUE_TTL = 1800  # 30 minutes — matches PRINT_OPTIONS_TTL

# CUPS binary paths — resolved once at startup to avoid repeated filesystem scans
LP_BIN: str | None = shutil.which("lp")
LPSTAT_BIN: str | None = shutil.which("lpstat")
CANCEL_BIN: str | None = shutil.which("cancel")

COPY_OPTIONS: dict[str, int] = {"2x": 2, "3x": 3, "4x": 4}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_cups_server() -> str:
    """Return the configured CUPS server host. Raises if not set."""
    server = os.getenv("CUPS_SERVER")
    if not server:
        raise RuntimeError("CUPS_SERVER environment variable is not set")
    return server


def get_printer_name() -> str:
    """Return the configured printer name. Raises if not set."""
    printer = os.getenv("PRINTER_NAME")
    if not printer:
        raise RuntimeError("PRINTER_NAME environment variable is not set")
    return printer


def get_allowed_chat_ids() -> list[int]:
    """Parse allowed chat IDs from environment variable (comma or space separated).

    Chat IDs are permanent numeric identifiers assigned by Telegram and cannot
    be changed or spoofed, making them reliable for access control.
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


def get_print_options(chat_id: int) -> dict:
    """Return print options for a chat, respecting the TTL and extending it on use.

    Returns defaults if no options are set or the options have expired.
    """
    entry = print_options.get(chat_id)
    if entry and (time.monotonic() - entry.get("ts", 0)) < PRINT_OPTIONS_TTL:
        entry["ts"] = time.monotonic()  # Extend the session
        return entry
    return {"color": True, "copies": 1, "media": "A4", "number_up": 1}


async def run_cups_command(cmd: list[str], timeout: int = 5) -> tuple[str, str, int]:
    """Run a CUPS CLI command and return (stdout, stderr, returncode).

    Guarantees the subprocess is killed and reaped if the timeout expires,
    preventing zombie processes and fd leaks.
    """
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    return stdout.decode(), stderr.decode(), process.returncode


# ---------------------------------------------------------------------------
# Bot text constants
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "📠 *PimNext Commands*\n\n"
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
    "  `a4`, `a5` — specific paper size\n"
    "  `half` — queue files and print 2 per sheet\n"
    "  `print` — flush queued half-mode files now\n"
    "  `bw half` — B&W half-sheet (common combo)\n"
    "  `bw 2x a5` — combine options\n\n"
    "_Settings persist for 30 minutes._"
)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "👋 Welcome to *PimNext*!\n\n"
        "Send me a photo or document and I'll print it for you.\n\n"
        "Use /help to see all available commands.",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report whether the configured printer is reachable via CUPS."""
    if not LPSTAT_BIN:
        await update.effective_message.reply_text(
            "⚠️ CUPS client tools (`lpstat`) not found on this system.",
            parse_mode="Markdown",
        )
        return

    try:
        server = get_cups_server()
        cmd = [LPSTAT_BIN, "-h", server, "-p"]
        stdout, stderr, returncode = await run_cups_command(cmd, timeout=5)

        if returncode == 0:
            if stdout.strip():
                msg = f"🟢 Printer is available:\n```\n{stdout.strip()}\n```"
            else:
                msg = "🟡 No printers are currently registered on the server."
        else:
            err = stderr.strip()[:MAX_STDERR_LENGTH]
            msg = f"🔴 Could not reach printer server:\n`{err or 'Unknown error'}`"

    except asyncio.TimeoutError:
        msg = "⚠️ Printer status check timed out."
    except RuntimeError as e:
        msg = f"⚠️ Configuration error: {e}"

    await update.effective_message.reply_text(msg, parse_mode="Markdown")


async def jobs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current CUPS print queue."""
    if not LPSTAT_BIN:
        await update.effective_message.reply_text(
            "⚠️ CUPS client tools (`lpstat`) not found on this system.",
            parse_mode="Markdown",
        )
        return

    try:
        server = get_cups_server()
        cmd = [LPSTAT_BIN, "-h", server, "-o"]
        stdout, stderr, returncode = await run_cups_command(cmd, timeout=5)

        if returncode == 0:
            msg = (
                f"🖨️ Print queue:\n```\n{stdout.strip()}\n```"
                if stdout.strip()
                else "📭 No jobs in queue"
            )
        else:
            err = stderr.strip()[:MAX_STDERR_LENGTH]
            msg = f"⚠️ Could not fetch queue: {err or 'Unknown error'}"

    except asyncio.TimeoutError:
        msg = "⚠️ Print queue check timed out."
    except RuntimeError as e:
        msg = f"⚠️ Configuration error: {e}"

    await update.effective_message.reply_text(msg, parse_mode="Markdown")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel all pending print jobs."""
    if not CANCEL_BIN:
        await update.effective_message.reply_text(
            "⚠️ CUPS client tools (`cancel`) not found on this system.",
            parse_mode="Markdown",
        )
        return

    try:
        server = get_cups_server()
        cmd = [CANCEL_BIN, "-h", server, "-a"]
        _, stderr, returncode = await run_cups_command(cmd, timeout=5)

        if returncode == 0:
            msg = "🗑️ All print jobs cancelled"
        else:
            err = stderr.strip()[:MAX_STDERR_LENGTH]
            msg = f"⚠️ Could not cancel jobs: {err or 'Unknown error'}"

    except asyncio.TimeoutError:
        msg = "⚠️ Cancel command timed out."
    except RuntimeError as e:
        msg = f"⚠️ Configuration error: {e}"

    await update.effective_message.reply_text(msg)


async def clean(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Clear all half-mode queues — their files will be removed by perform_cleanup below
    half_queue.clear()
    # Offload blocking I/O to a thread pool to avoid stalling the event loop
    removed = await perform_cleanup_async()
    await update.effective_message.reply_text(
        f"🗑️ Cleaned up {removed} cached file(s) from the data folder."
    )


async def set_print_options(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parse print option keywords from a text message."""
    chat_id = update.effective_chat.id
    text = update.effective_message.text.strip().lower()

    # "print" is an action keyword — flush the half-mode queue immediately.
    if text == "print":
        entry = half_queue.get(chat_id)
        if not entry or not entry.get("files"):
            await update.effective_message.reply_text(
                "❓ No files queued. Send a file with `half` mode active to queue it.",
                parse_mode="Markdown",
            )
            return
        opts = get_print_options(chat_id)
        await _flush_half_queue(update, context, chat_id, opts)
        return

    color = True
    copies = 1
    media = "A4"
    number_up = 1
    valid = True

    tokens = text.split()
    for token in tokens:
        if token in ("bw", "gray"):
            color = False
        elif token in COPY_OPTIONS:
            copies = COPY_OPTIONS[token]
        elif token == "a4":
            media = "A4"
        elif token == "a5":
            media = "A5"
        elif token == "half":
            media = "A4"
            number_up = 2
        else:
            valid = False

    if not valid or not tokens:
        await update.effective_message.reply_text(
            "❓ Unknown option. Use: `bw`, `2x`, `3x`, `4x`, `a4`, `a5`, `half`, `print`",
            parse_mode="Markdown",
        )
        return

    print_options[chat_id] = {
        "color": color, 
        "copies": copies, 
        "media": media,
        "number_up": number_up,
        "ts": time.monotonic()
    }

    # If switching away from half mode, discard any pending queued files.
    if number_up != 2:
        old_entry = half_queue.pop(chat_id, None)
        if old_entry and old_entry.get("files"):
            for fp in old_entry["files"]:
                try:
                    os.remove(fp)
                    logger.info("Cleared stale half-queue file on option change: %s", fp)
                except OSError as e:
                    logger.warning("Could not remove queued file %s: %s", fp, e)
            notice = f"⚠️ {len(old_entry['files'])} queued file(s) cleared (half mode disabled).\n"
        else:
            notice = ""
    else:
        notice = ""

    mode = "B&W" if not color else "Color"
    count = f"{copies} copies" if copies > 1 else "1 copy"
    format_str = f"{media} (Half Sheet)" if number_up == 2 else media
    await update.effective_message.reply_text(f"{notice}⚙️ Settings updated: {mode}, {count}, {format_str}\n_(Active for 30m)_", parse_mode="Markdown")


async def _get_file_info(update: Update) -> tuple | None:
    """Validate and retrieve a file from an incoming message.

    Returns (file_obj, orig_ext) on success, or None after sending an error reply.
    """
    msg = update.effective_message

    if msg.photo:
        # `if msg.photo:` guarantees a non-empty list here, so [-1] is safe.
        # Telegram sends photos ordered smallest → largest; pick the largest with a known size.
        # Fall back to the last entry (largest by Telegram's ordering) if all sizes are None.
        photos_with_size = [p for p in msg.photo if p.file_size is not None]
        photo = max(photos_with_size, key=lambda x: x.file_size) if photos_with_size else msg.photo[-1]
        if photo.file_size and photo.file_size > MAX_FILE_BYTES:
            await msg.reply_text(
                f"❌ File too large ({photo.file_size // 1024 // 1024} MB). Maximum is 20 MB."
            )
            return None
        file = await photo.get_file()
        return file, ".jpg"

    if msg.document:
        doc = msg.document
        orig_ext = os.path.splitext(doc.file_name)[1].lower() if doc.file_name else ""
        if orig_ext not in PRINTABLE_EXTENSIONS:
            await msg.reply_text(
                f"❌ Unsupported file type `{orig_ext or '(none)'}`. "
                f"Supported: {PRINTABLE_EXTENSIONS_DISPLAY}",
                parse_mode="Markdown",
            )
            return None
        if doc.file_size and doc.file_size > MAX_FILE_BYTES:
            await msg.reply_text(
                f"❌ File too large ({doc.file_size // 1024 // 1024} MB). Maximum is 20 MB."
            )
            return None
        file = await doc.get_file()
        return file, orig_ext

    await msg.reply_text("❌ Could not find a printable file in your message.")
    return None


async def _flush_half_queue(
    update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, opts: dict
) -> None:
    """Rate-limit, then print all files queued for this chat in half mode.

    Clears the queue on success.  Leaves the queue intact when rate-limited so
    the user can retry by sending `print` after the cooldown expires.
    """
    entry = half_queue.get(chat_id)
    if not entry or not entry.get("files"):
        await update.effective_message.reply_text("❓ No files are queued for printing.")
        return

    # Files in the half queue were always queued in half mode; ensure opts reflect that
    # even if the 30-minute TTL has since expired and get_print_options returned defaults.
    if opts.get("number_up", 1) != 2:
        opts = dict(opts)
        opts["number_up"] = 2

    # ── Rate limiting ────────────────────────────────────────────────────────
    now = time.monotonic()
    elapsed = now - last_print_time.get(chat_id, 0)
    if elapsed < PRINT_COOLDOWN:
        remaining = int(PRINT_COOLDOWN - elapsed)
        file_count = len(entry["files"])
        await update.effective_message.reply_text(
            f"⏳ Please wait {remaining}s before printing. "
            f"Your {file_count} queued file(s) are ready — send `print` when the cooldown ends.",
            parse_mode="Markdown",
        )
        return

    files = list(entry["files"])
    half_queue.pop(chat_id, None)
    last_print_time[chat_id] = time.monotonic()

    file_count = len(files)
    sheet_count = (file_count + 1) // 2

    try:
        await print_file(
            files,
            color=opts["color"],
            copies=opts["copies"],
            media=opts["media"],
            number_up=opts["number_up"],
        )
        await notify_homeassistant(
            file_name=", ".join(os.path.basename(fp) for fp in files),
            chat_id=chat_id,
            copies=opts["copies"],
            color=opts["color"],
        )
        await update.effective_message.reply_text(
            f"✅ Sent {file_count} file(s) to printer! "
            f"(~{sheet_count} sheet{'s' if sheet_count != 1 else ''})"
            + (
                "\n_(Half mode still active — send your next file when ready.)_"
                if opts.get("number_up", 1) == 2 else ""
            ),
            parse_mode="Markdown",
        )

    except RuntimeError as e:
        logger.error("Print failed: %s", e)
        cmd_used = getattr(e, "cmd", None)
        msg = f"❌ Print failed: {e}"
        if cmd_used:
            msg += f"\n\nCommand used:\n{cmd_used}"
        await update.effective_message.reply_text(msg)

    except Exception as e:
        logger.exception("Unexpected error during print: %s", e)
        await update.effective_message.reply_text(f"❌ Unexpected error: {e}")

    finally:
        for fp in files:
            try:
                os.remove(fp)
                logger.info("Cleaned up %s", fp)
            except OSError as exc:
                logger.warning("Could not remove %s: %s", fp, exc)


async def print_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming photo or document and send it to the printer."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    logger.info(
        "Received message from user_id=%s username=%s",
        user.id,
        user.username or "N/A",
    )

    opts = get_print_options(chat_id)

    # ── Half mode: queue files and auto-print every 2 ────────────────────────
    if opts["number_up"] == 2:
        file_info = await _get_file_info(update)
        if file_info is None:
            return

        file_obj, orig_ext = file_info
        os.makedirs(DATA_DIR, exist_ok=True)
        file_path = os.path.join(DATA_DIR, f"{uuid.uuid4().hex}{orig_ext}")
        await file_obj.download_to_drive(file_path)
        logger.info("Half-mode: queued file at %s", file_path)

        entry = half_queue.setdefault(chat_id, {"files": [], "ts": 0})
        entry["files"].append(file_path)
        entry["ts"] = time.monotonic()

        file_count = len(entry["files"])
        sheet_count = (file_count + 1) // 2

        if file_count % 2 == 1:
            # Odd count — waiting for a pairing file
            await update.effective_message.reply_text(
                f"📄 File {file_count} queued "
                f"(~{sheet_count} sheet{'s' if sheet_count != 1 else ''} so far). "
                f"Send another file to fill this sheet, or send `print` to print now.",
                parse_mode="Markdown",
            )
        else:
            # Even count — auto-flush the queue
            await _flush_half_queue(update, context, chat_id, opts)
        return

    # ── Normal mode ──────────────────────────────────────────────────────────
    # Rate limiting
    now = time.monotonic()
    elapsed = now - last_print_time.get(chat_id, 0)
    if elapsed < PRINT_COOLDOWN:
        remaining = int(PRINT_COOLDOWN - elapsed)
        await update.effective_message.reply_text(
            f"⏳ Please wait {remaining}s before sending another print job."
        )
        return

    # Claim the rate-limit slot before yielding to the event loop so a second
    # concurrent message for the same chat cannot slip through the check above.
    last_print_time[chat_id] = time.monotonic()

    file_info = await _get_file_info(update)
    if file_info is None:
        # No valid file — not a real print attempt, release the rate-limit slot.
        last_print_time.pop(chat_id, None)
        return

    file_obj, orig_ext = file_info

    # Use a UUID-based filename to prevent collisions under concurrent prints
    os.makedirs(DATA_DIR, exist_ok=True)
    file_path = os.path.join(DATA_DIR, f"{uuid.uuid4().hex}{orig_ext}")
    await file_obj.download_to_drive(file_path)
    logger.info("File saved at %s", file_path)

    try:
        await print_file(
            [file_path],
            color=opts["color"],
            copies=opts["copies"],
            media=opts["media"],
            number_up=opts["number_up"],
        )

        # Fire HA webhook in the try block (after confirmed print, before reply)
        await notify_homeassistant(
            file_name=os.path.basename(file_path),
            chat_id=chat_id,
            copies=opts["copies"],
            color=opts["color"],
        )
        await update.effective_message.reply_text("✅ Sent to printer!")

    except RuntimeError as e:
        logger.error("Print failed: %s", e)
        cmd_used = getattr(e, "cmd", None)
        msg = f"❌ Print failed: {e}"
        if cmd_used:
            msg += f"\n\nCommand used:\n{cmd_used}"
        await update.effective_message.reply_text(msg)

    except Exception as e:
        logger.exception("Unexpected error during print: %s", e)
        await update.effective_message.reply_text(
            f"❌ Unexpected error: {e}"
        )

    finally:
        try:
            os.remove(file_path)
            logger.info("Cleaned up %s", file_path)
        except OSError as e:
            logger.warning("Could not remove %s: %s", file_path, e)


# ---------------------------------------------------------------------------
# Core print logic
# ---------------------------------------------------------------------------

async def print_file(file_paths: list[str], color: bool = True, copies: int = 1, media: str = "A4", number_up: int = 1) -> str:
    """Send one or more files to the printer using lp.

    Multiple files are passed as a single lp job so that, combined with
    number-up=2 (half mode), CUPS places both files on the same physical sheet.

    Always passes -h <CUPS_SERVER> and -d <PRINTER_NAME> explicitly.
    Both environment variables are required — raises RuntimeError if missing.

    Returns the shell command string that was executed.
    """
    if not LP_BIN:
        raise RuntimeError("lp command not found — is cups-client installed?")

    if not file_paths:
        raise RuntimeError("Internal error: print_file called with empty file list")

    server = get_cups_server()
    printer = get_printer_name()

    # Build: lp -h <server> -d <printer> -o fit-to-page -o media=<media> [options] <file(s)>
    cmd = [LP_BIN, "-h", server, "-d", printer, "-o", "fit-to-page", "-o", f"media={media}"]

    if number_up > 1:
        cmd += ["-o", f"number-up={number_up}"]
    if not color:
        # ColorModel=Gray is standard CUPS; CNColorMode=mono is Canon UFRII specific
        cmd += ["-o", "ColorModel=Gray", "-o", "CNColorMode=mono"]
    if copies > 1:
        cmd += ["-n", str(copies)]
    cmd.extend(file_paths)

    cmd_str = " ".join(cmd)
    logger.info("Shell command: %s", cmd_str)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError:
        # Kill and reap the process to prevent zombie / fd leak
        process.kill()
        await process.wait()
        ex = RuntimeError("lp command timed out after 30 seconds")
        ex.cmd = cmd_str  # type: ignore[attr-defined]
        raise ex

    if process.returncode != 0:
        err_str = stderr.decode().strip()[:MAX_STDERR_LENGTH]
        logger.error(
            "lp failed (returncode=%s) stderr: %s",
            process.returncode,
            stderr.decode(),
        )
        ex = RuntimeError(err_str or "Print command failed")
        ex.cmd = cmd_str  # type: ignore[attr-defined]
        raise ex

    logger.info("lp stdout: %s", stdout.decode().strip())
    return cmd_str


# ---------------------------------------------------------------------------
# Home Assistant integration
# ---------------------------------------------------------------------------

async def notify_homeassistant(
    file_name: str, chat_id: int, copies: int, color: bool
) -> None:
    """Fire a Home Assistant event after a successful print (best-effort, non-blocking).

    Uses httpx (async) so the event loop is never stalled waiting for the HA response.
    """
    ha_url = os.getenv("HA_URL")
    ha_token = os.getenv("HA_TOKEN")
    if not ha_url or not ha_token:
        return

    url = f"{ha_url}/api/events/pimnext_job_sent"
    payload = {
        "file_name": file_name,
        "chat_id": chat_id,
        "copies": copies,
        "color": color,
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {ha_token}"},
                timeout=3,
            )
            response.raise_for_status()
        logger.info("Home Assistant notified: %s", url)
    except Exception as e:
        logger.warning("Home Assistant notification failed: %s", e)


# ---------------------------------------------------------------------------
# Startup / lifecycle
# ---------------------------------------------------------------------------

async def post_init(application) -> None:
    """Register bot commands and start background tasks."""
    await application.bot.set_my_commands([
        BotCommand("start", "Show the welcome message"),
        BotCommand("help", "Show available commands"),
        BotCommand("status", "Check printer availability"),
        BotCommand("jobs", "Show the print queue"),
        BotCommand("cancel", "Cancel all print jobs"),
        BotCommand("clean", "Delete cached files (allowed users only)"),
    ])

    # Start periodic cleanup task — track it so exceptions are not silently lost
    task = asyncio.create_task(cleanup_task())
    task.add_done_callback(_on_cleanup_task_done)
    logger.info("Periodic cleanup task started (6h interval)")


def _on_cleanup_task_done(task: asyncio.Task) -> None:
    """Log if the background cleanup task crashes unexpectedly."""
    if not task.cancelled() and task.exception():
        logger.error("Cleanup task crashed: %s", task.exception())


async def cleanup_task() -> None:
    """Background task: evict stale print options and remove leftover data files."""
    while True:
        await asyncio.sleep(6 * 3600)  # every 6 hours

        # Evict expired print_options entries that were never consumed (leak prevention)
        now = time.monotonic()
        expired_chats = [
            cid for cid, entry in print_options.items()
            if (now - entry.get("ts", 0)) >= PRINT_OPTIONS_TTL
        ]
        for cid in expired_chats:
            print_options.pop(cid, None)
        if expired_chats:
            logger.info("Evicted %d expired print option(s).", len(expired_chats))

        # Evict stale last_print_time entries (prevents unbounded growth)
        stale_rate = [
            cid for cid, ts in last_print_time.items()
            if (now - ts) >= PRINT_OPTIONS_TTL
        ]
        for cid in stale_rate:
            last_print_time.pop(cid, None)
        if stale_rate:
            logger.info("Evicted %d stale rate-limit entry(ies).", len(stale_rate))

        # Evict expired half-queue entries and delete their files
        expired_half = [
            cid for cid, entry in half_queue.items()
            if (now - entry.get("ts", 0)) >= HALF_QUEUE_TTL
        ]
        for cid in expired_half:
            entry = half_queue.pop(cid, None)
            if entry:
                for fp in entry.get("files", []):
                    try:
                        os.remove(fp)
                        logger.info("Evicted stale half-queue file: %s", fp)
                    except OSError:
                        pass
        if expired_half:
            logger.info("Evicted %d expired half-queue(s).", len(expired_half))

        # Remove leftover downloaded files, but protect files still in an active queue
        logger.info("Running periodic data cleanup...")
        try:
            active_files: frozenset[str] = frozenset(
                fp
                for entry in half_queue.values()
                for fp in entry.get("files", [])
            )
            removed = await perform_cleanup_async(frozenset(active_files))
            logger.info("Periodic cleanup removed %d file(s).", removed)
        except Exception as e:
            logger.error("Periodic cleanup failed: %s", e)


def perform_cleanup(skip_paths: frozenset[str] | None = None) -> int:
    """Delete all cached files from the data directory. Returns count removed.

    skip_paths: Optional set of absolute file paths to preserve (e.g. active
    half-queue files that are still awaiting pairing).

    Synchronous — safe to call at startup before the event loop starts.
    Use perform_cleanup_async() from async contexts.
    """
    skip_paths = skip_paths or frozenset()
    removed = 0
    if os.path.exists(DATA_DIR):
        for filename in os.listdir(DATA_DIR):
            filepath = os.path.join(DATA_DIR, filename)
            if filepath in skip_paths:
                continue
            try:
                if os.path.isfile(filepath):
                    os.remove(filepath)
                    removed += 1
            except OSError as e:
                logger.error("Error removing %s: %s", filepath, e)
    return removed


async def perform_cleanup_async(skip_paths: frozenset[str] | None = None) -> int:
    """Async wrapper for perform_cleanup — offloads blocking I/O to a thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, perform_cleanup, skip_paths)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.getenv("TOKEN")
    if not token:
        logger.error("TOKEN environment variable is not set")
        raise SystemExit("TOKEN environment variable is required")

    allowed_chat_ids = get_allowed_chat_ids()

    # Log configuration at startup for easy debugging
    logger.info("PimNext v%s starting...", VERSION)
    logger.info(
        "Configuration: CUPS_SERVER=%s  PRINTER_NAME=%s",
        os.getenv("CUPS_SERVER", "(not set)"),
        os.getenv("PRINTER_NAME", "(not set)"),
    )
    logger.info("Allowed chat IDs: %s", allowed_chat_ids or "ALL")

    # Warn early if any CUPS binary is missing (won't prevent startup but commands will fail)
    if not LP_BIN:
        logger.warning("lp binary not found — printing will not work.")
    if not LPSTAT_BIN:
        logger.warning("lpstat binary not found — /status and /jobs will not work.")
    if not CANCEL_BIN:
        logger.warning("cancel binary not found — /cancel will not work.")

    # Clean up any stale files from a previous run (sync — before event loop starts)
    logger.info("Performing startup cleanup...")
    perform_cleanup()

    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    # Public commands (no access restriction)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status))

    # Restricted commands and message handlers
    if allowed_chat_ids:
        chat_id_filter = filters.Chat(chat_id=allowed_chat_ids)
    else:
        logger.warning(
            "ALLOWED_CHAT_IDS is not set — all Telegram users can print. "
            "Set this variable to restrict access."
        )
        chat_id_filter = filters.ALL

    application.add_handler(CommandHandler("jobs", jobs_command, filters=chat_id_filter))
    application.add_handler(CommandHandler("cancel", cancel_command, filters=chat_id_filter))
    application.add_handler(CommandHandler("clean", clean, filters=chat_id_filter))

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

    application.run_polling()


if __name__ == "__main__":
    main()
