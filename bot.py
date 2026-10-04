import asyncio
import json
import logging
import os
import shutil
import sys
import time
import uuid
import warnings
from dataclasses import asdict, dataclass, field, replace

import httpx
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

# LOG_LEVEL falls back to INFO when unset or unrecognised. The warning goes to
# stdout because logging is not configured yet — a silent fallback was the 1.0.6
# bug, so do not trade it away for a shorter block.
_raw_level = os.getenv("LOG_LEVEL", "INFO").upper()
_level = getattr(logging, _raw_level, None)
if not isinstance(_level, int):
    print(f"WARNING: Invalid LOG_LEVEL={_raw_level!r} — defaulting to INFO.")
    _level = logging.INFO

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=_level,
)

# Suppress noisy library loggers — only show WARNING and above from these
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

logger = logging.getLogger("notanext")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fallback used only if the VERSION file is missing or unreadable. Named (not
# inlined) so scripts/bump_version.py has a stable assignment to rewrite on
# every bump — do not phrase this comment as `DEFAULT_VERSION = "1.3.0"` or the
# bump script's regex will rewrite the comment too.
DEFAULT_VERSION = "1.3.0"


def _load_version() -> str:
    version_file = os.path.join(os.path.dirname(__file__), "VERSION")
    try:
        with open(version_file, "r", encoding="utf-8") as f:
            ver = f.read().strip()
            if ver:
                return ver
    except OSError:
        pass
    return DEFAULT_VERSION


VERSION = _load_version()
DATA_DIR = "data"
PREFERENCES_FILE = os.path.join(DATA_DIR, "preferences.json")
CORRUPT_PREFERENCES_FILE = os.path.join(DATA_DIR, "preferences.json.corrupt")

# Merge child script path and timeout
MERGE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "merge_pdf.py")
MERGE_TIMEOUT = 60
MAX_HALF_QUEUE_FILES = 10

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

# Extensions merge_to_pdf() can combine into a single document. Half mode always
# merges before printing, so anything outside this set is rejected up front
# rather than silently falling back to a non-2-up multi-file lp job.
MERGEABLE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".pdf"}
MERGEABLE_EXTENSIONS_DISPLAY = ", ".join(sorted(MERGEABLE_EXTENSIONS))

# Session and rate-limit timeouts
PRINT_COOLDOWN = 10  # minimum seconds between accepted print jobs
SESSION_TTL = 1800  # 30 minutes for temporary overrides & half-queue

# CUPS binary paths — resolved once at startup to avoid repeated filesystem scans
LP_BIN: str | None = shutil.which("lp")
LPSTAT_BIN: str | None = shutil.which("lpstat")
CANCEL_BIN: str | None = shutil.which("cancel")

COPY_OPTIONS: dict[str, int] = {"1x": 1, "2x": 2, "3x": 3, "4x": 4}

# Conversation states for the preference-setting wizard
PREF_COLOR, PREF_MODE, PREF_PAPER = range(3)

# Cap on the number of stored per-chat preference entries. Keeps preferences.json
# bounded and stops an open wizard from being used to exhaust the store.
MAX_PREFERENCES = 10


# ---------------------------------------------------------------------------
# Dataclasses & State
# ---------------------------------------------------------------------------

@dataclass
class PrintOptions:
    """One chat's print settings.

    The four leading fields make up the persisted profile. `ts` is the monotonic
    timestamp of the temporary 30-minute session and is never written to disk.
    """

    color: bool = True
    copies: int = 1
    media: str = "A4"
    number_up: int = 1
    ts: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> "PrintOptions":
        """Build a profile from a persisted mapping, filling in missing keys."""
        defaults = cls()
        return cls(
            color=bool(data.get("color", defaults.color)),
            copies=int(data.get("copies", defaults.copies)),
            media=str(data.get("media", defaults.media)),
            number_up=int(data.get("number_up", defaults.number_up)),
        )

    def to_dict(self) -> dict:
        """Serialise the persisted profile, dropping the session timestamp."""
        data = asdict(self)
        data.pop("ts", None)
        return data

    @property
    def color_label(self) -> str:
        """Human-readable colour mode, used in every user-facing reply."""
        return "Color" if self.color else "Gray (B&W)"

    @property
    def mode_label(self) -> str:
        """Human-readable sheet mode, used in every user-facing reply."""
        return "Half sheet" if self.number_up == 2 else "Normal"


@dataclass
class HalfQueueEntry:
    """Queued files and timestamp for a half-mode print session."""

    files: list[str] = field(default_factory=list)
    ts: float = 0.0


# Keyboards pre-built once at startup to avoid runtime allocations
COLOR_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("🎨 Color", callback_data="pref_color_yes"),
            InlineKeyboardButton("⬛ Gray (B&W)", callback_data="pref_color_no"),
        ]
    ]
)

MODE_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("📄 Normal (full page)", callback_data="pref_mode_normal"),
            InlineKeyboardButton("📑 Half sheet (2 per page)", callback_data="pref_mode_half"),
        ]
    ]
)

PAPER_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("A4", callback_data="pref_paper_A4"),
            InlineKeyboardButton("A5", callback_data="pref_paper_A5"),
        ]
    ]
)

# Chat state (in-memory)
print_options: dict[int, PrintOptions] = {}
last_print_time: dict[int, float] = {}
half_queue: dict[int, HalfQueueEntry] = {}
user_preferences: dict[str, PrintOptions] = {}
in_flight_paths: set[str] = set()


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


def parse_option_tokens(tokens: list[str], opts: PrintOptions) -> bool:
    """Apply print-option keywords to `opts` in place.

    Returns False if any token was unrecognised — the caller rejects the whole
    message in that case, so a half-applied `opts` is never stored.
    """
    for token in tokens:
        if token in ("bw", "gray"):
            opts.color = False
        elif token == "color":
            opts.color = True
        elif token in COPY_OPTIONS:
            opts.copies = COPY_OPTIONS[token]
        elif token == "a4":
            opts.media = "A4"
        elif token == "a5":
            opts.media = "A5"
        elif token in ("half", "2up"):
            opts.number_up = 2
        elif token in ("normal", "full", "single", "1up"):
            opts.number_up = 1
        else:
            return False
    return True


def cooldown_remaining(chat_id: int) -> int:
    """Seconds left on this chat's print cooldown, or 0 if it may print now."""
    elapsed = time.monotonic() - last_print_time.get(chat_id, 0)
    # Round up so the last fractional second still blocks (and never reports "0s").
    return int(PRINT_COOLDOWN - elapsed) + 1 if elapsed < PRINT_COOLDOWN else 0


def get_print_options(chat_id: int) -> PrintOptions:
    """Return print options for a chat, respecting the TTL and extending it on use.

    Falls back to the chat's saved persistent defaults (or system defaults) if no
    session options are active.
    """
    entry = print_options.get(chat_id)
    if entry and (time.monotonic() - entry.ts) < SESSION_TTL:
        entry.ts = time.monotonic()  # Extend the session
        return entry
    return get_default_preferences(chat_id)


def get_default_preferences(chat_id: int) -> PrintOptions:
    """Return the saved persistent default preferences for a chat, or system defaults."""
    saved = user_preferences.get(str(chat_id))
    if saved:
        return replace(saved, ts=0.0)
    return PrintOptions()


def discard_half_queue(chat_id: int) -> int:
    """Discard pending half-queue files for a chat. Returns count of removed files."""
    old_entry = half_queue.pop(chat_id, None)
    if old_entry and old_entry.files:
        for fp in old_entry.files:
            try:
                os.remove(fp)
                logger.info("Cleared half-queue file: %s", fp)
            except OSError as e:
                logger.warning("Could not remove queued file %s: %s", fp, e)
        return len(old_entry.files)
    return 0


def get_half_queue(chat_id: int) -> HalfQueueEntry | None:
    """Return active half-queue entry for chat_id, discarding files if expired."""
    entry = half_queue.get(chat_id)
    if not entry:
        return None
    if (time.monotonic() - entry.ts) >= SESSION_TTL:
        discard_half_queue(chat_id)
        return None
    return entry


def load_preferences() -> None:
    """Load per-chat persistent preferences from disk into memory.

    Trims the loaded data to MAX_PREFERENCES so an oversized file from an older
    build doesn't exceed the cap at runtime. On corrupt JSON or shape failure,
    the corrupt file is quarantined to preferences.json.corrupt and logged at ERROR.
    """
    global user_preferences
    if not os.path.exists(PREFERENCES_FILE):
        return
    try:
        with open(PREFERENCES_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Preferences root must be a dict, got {type(data).__name__}")
        if len(data) > MAX_PREFERENCES:
            # Keep only the first MAX_PREFERENCES entries (arbitrary but deterministic)
            data = dict(list(data.items())[:MAX_PREFERENCES])
            logger.warning("Preferences file exceeded cap. Trimmed to %d entries.", len(data))
        loaded: dict[str, PrintOptions] = {}
        for key, value in data.items():
            try:
                loaded[key] = PrintOptions.from_dict(value)
            except (AttributeError, TypeError, ValueError) as e:
                logger.warning("Skipping malformed preference entry %r: %s", key, e)
        user_preferences = loaded
        logger.info("Loaded preferences for %d chat(s).", len(user_preferences))
    except Exception as e:
        logger.error("Could not load preferences file, quarantining to %s: %s", CORRUPT_PREFERENCES_FILE, e)
        try:
            os.replace(PREFERENCES_FILE, CORRUPT_PREFERENCES_FILE)
        except OSError as rename_err:
            logger.error("Failed to rename corrupt preferences file: %s", rename_err)
        user_preferences = {}


def save_preferences() -> None:
    """Persist the in-memory user_preferences dict to disk atomically."""
    tmp = PREFERENCES_FILE + ".tmp"
    in_flight_paths.add(tmp)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump({k: v.to_dict() for k, v in user_preferences.items()}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PREFERENCES_FILE)
        logger.debug("Preferences saved (%d chat(s)).", len(user_preferences))
    except Exception as e:
        logger.error("Could not save preferences: %s", e)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    finally:
        in_flight_paths.discard(tmp)


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


async def run_cups_query(
    binary: str | None, tool: str, flags: list[str], action: str
) -> tuple[str | None, str | None]:
    """Run a CUPS query against the configured server, returning (stdout, error).

    Exactly one of the two is not None. Shared by /status, /jobs and /cancel so
    the binary check, `-h <server>` wiring, timeout handling and stderr
    truncation live in one place instead of once per command handler.

    Neither return value carries Markdown formatting — CUPS stderr and stdout
    (job titles, printer descriptions) are outside our control and a stray
    backtick/underscore/asterisk in either breaks Telegram's Markdown parser,
    which fails the whole reply instead of degrading gracefully. Callers must
    not send these with parse_mode="Markdown".
    """
    if not binary:
        return None, f"⚠️ CUPS client tools ({tool}) not found on this system."

    try:
        server = get_cups_server()
        stdout, stderr, returncode = await run_cups_command([binary, "-h", server, *flags])
    except asyncio.TimeoutError:
        return None, f"⚠️ {action} timed out."
    except RuntimeError as e:
        return None, f"⚠️ Configuration error: {e}"

    if returncode != 0:
        err = stderr.strip()[:MAX_STDERR_LENGTH] or "Unknown error"
        return None, f"⚠️ {action} failed: {err}"

    return stdout, None


# ---------------------------------------------------------------------------
# Bot text constants
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "📠 *NotaNext Commands*\n\n"
    "/start — Show the welcome message\n"
    "/help — Show this help message\n"
    "/preferences — Set your default printing preferences\n"
    "/status — Check printer availability\n"
    "/jobs — Show the print queue\n"
    "/cancel — Cancel all jobs on this printer\n"
    "/clean — Delete cached files and clear your queue\n\n"
    "Send a *photo* or *document* to print it.\n\n"
    "*Print options* — send before your file:\n"
    "  `bw` or `gray` — black & white\n"
    "  `color` — full colour\n"
    "  `1x`, `2x`, `3x`, `4x` — number of copies\n"
    "  `a4`, `a5` — paper size\n"
    "  `half` or `2up` — queue files and print 2 per sheet\n"
    "  `normal`, `full`, `single` or `1up` — one page per sheet\n"
    "  `print` — flush queued half-mode files now\n"
    "  `bw 2x a5` — combine options\n\n"
    "_Per-session settings persist for 30 minutes, then fall back to your saved defaults._"
)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Welcome the user and launch the default-preference wizard."""
    chat_id = update.effective_chat.id
    saved = user_preferences.get(str(chat_id))

    welcome = "👋 Welcome to *NotaNext*!\n\nSend me a photo or document and I'll print it for you.\n\n"
    if saved:
        welcome += (
            f"📌 *Current defaults:* {saved.color_label} · {saved.mode_label} · {saved.media}\n\n"
            "Update your default printing preferences below, or use /help to get started.\n\n"
        )
    else:
        welcome += "Let's set up your default printing preferences.\n\n"

    welcome += "*Color mode?*"

    await update.effective_message.reply_text(
        welcome,
        parse_mode="Markdown",
        reply_markup=COLOR_KEYBOARD,
    )
    return PREF_COLOR


async def preferences_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the default-preference wizard directly."""
    await update.effective_message.reply_text(
        "⚙️ *Set Default Preferences*\n\n*Color mode?*",
        parse_mode="Markdown",
        reply_markup=COLOR_KEYBOARD,
    )
    return PREF_COLOR


async def pref_color_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle color-mode selection and ask for sheet mode."""
    query = update.callback_query
    await query.answer()

    draft = PrintOptions(color=query.data == "pref_color_yes")
    context.user_data["pref_draft"] = draft

    await query.edit_message_text(
        f"✅ Color mode: *{draft.color_label}*\n\n*Sheet mode?*",
        parse_mode="Markdown",
        reply_markup=MODE_KEYBOARD,
    )
    return PREF_MODE


async def pref_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle sheet-mode selection and ask for paper size."""
    query = update.callback_query
    await query.answer()

    draft = context.user_data.get("pref_draft") or PrintOptions()
    draft.number_up = 2 if query.data == "pref_mode_half" else 1
    context.user_data["pref_draft"] = draft

    await query.edit_message_text(
        f"✅ Color mode: *{draft.color_label}*\n✅ Sheet mode: *{draft.mode_label}*\n\n*Paper size?*",
        parse_mode="Markdown",
        reply_markup=PAPER_KEYBOARD,
    )
    return PREF_PAPER


async def pref_paper_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle paper-size selection, save preferences and confirm."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    draft = context.user_data.pop("pref_draft", None) or PrintOptions()
    draft.media = "A5" if query.data == "pref_paper_A5" else "A4"

    key = str(chat_id)

    # Enforce the cap: reject new entries (updates to existing entries always pass)
    if key not in user_preferences and len(user_preferences) >= MAX_PREFERENCES:
        await query.edit_message_text(
            f"⚠️ The preference store is full ({MAX_PREFERENCES} chat(s) already saved).",
        )
        return ConversationHandler.END

    user_preferences[key] = replace(draft, ts=0.0)
    save_preferences()

    # If saving normal mode, discard any pending half-queue files.
    if draft.number_up != 2:
        discard_half_queue(chat_id)

    # Apply the new defaults immediately for this active chat session.
    # Without this, a previously cached 30-minute override can keep using old
    # settings (e.g. normal mode) even though defaults were just saved.
    print_options[chat_id] = replace(draft, ts=time.monotonic())

    await query.edit_message_text(
        "✅ *Default preferences saved!*\n\n"
        f"  🎨 Color mode: *{draft.color_label}*\n"
        f"  📄 Sheet mode: *{draft.mode_label}*\n"
        f"  📐 Paper size: *{draft.media}*\n\n"
        "These will be used every time you print.\n"
        "Send text options like `bw` or `half` to override them temporarily.\n"
        "Use /preferences to change your defaults at any time.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def cancel_preferences(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the preference-setting wizard.

    /cancel is bound both here (as the wizard's fallback) and to cancel_command.
    While the wizard is open this handler wins, so the reply says how to reach
    the print-queue meaning of the command.
    """
    context.user_data.pop("pref_draft", None)
    await update.effective_message.reply_text(
        "❌ Preference setup cancelled.\n"
        "Send /cancel again to cancel pending print jobs."
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the command and print-option reference."""
    await update.effective_message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report whether the configured printer is reachable via CUPS."""
    stdout, error = await run_cups_query(LPSTAT_BIN, "lpstat", ["-p"], "Printer status check")

    if error:
        msg = error
    elif stdout.strip():
        msg = f"🟢 Printer is available:\n{stdout.strip()}"
    else:
        msg = "🟡 No printers are currently registered on the server."

    # No parse_mode: lpstat output is outside our control and a stray Markdown
    # character in it would fail the whole reply — see run_cups_query().
    await update.effective_message.reply_text(msg)


async def jobs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current CUPS print queue for the configured printer."""
    try:
        printer = get_printer_name()
    except RuntimeError as e:
        await update.effective_message.reply_text(f"⚠️ Configuration error: {e}")
        return

    stdout, error = await run_cups_query(LPSTAT_BIN, "lpstat", ["-o", printer], "Print queue check")

    if error:
        msg = error
    elif stdout.strip():
        msg = f"🖨️ Print queue:\n{stdout.strip()}"
    else:
        msg = "📭 No jobs in queue"

    # No parse_mode: job titles can come from other clients — see run_cups_query().
    await update.effective_message.reply_text(msg)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel all pending print jobs for the configured printer."""
    try:
        printer = get_printer_name()
    except RuntimeError as e:
        await update.effective_message.reply_text(f"⚠️ Configuration error: {e}")
        return

    _, error = await run_cups_query(CANCEL_BIN, "cancel", ["-a", printer], "Cancel command")
    # No parse_mode: `error` may carry raw CUPS stderr — see run_cups_query().
    await update.effective_message.reply_text(error or "🗑️ All print jobs cancelled")


async def clean(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete cached files from the data directory and clear caller's queued files."""
    chat_id = update.effective_chat.id
    # Clear only the calling chat's half-mode queue
    entry = half_queue.pop(chat_id, None)
    if entry:
        for fp in entry.files:
            try:
                os.remove(fp)
                logger.info("Cleared caller half-queue file on clean: %s", fp)
            except OSError:
                pass

    # Preserve other chats' queued files as well as in-flight paths
    other_queued = {
        fp
        for cid, q in half_queue.items()
        if cid != chat_id
        for fp in q.files
    }
    skip_set = frozenset(other_queued | in_flight_paths)
    removed = await perform_cleanup_async(skip_set, min_age=0)
    await update.effective_message.reply_text(
        f"🗑️ Cleaned up {removed} cached file(s) from the data folder."
    )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a plain text message: either a print-option update or the `print` action.

    `print` is an action keyword rather than a setting — it flushes whatever is
    currently queued in half mode. Everything else is parsed as option keywords,
    and a single unrecognised token rejects the whole message.
    """
    chat_id = update.effective_chat.id
    text = update.effective_message.text.strip().lower()

    # "print" is an action keyword — flush the half-mode queue immediately.
    if text == "print":
        entry = get_half_queue(chat_id)
        if not entry or not entry.files:
            await update.effective_message.reply_text(
                "❓ No files queued. Send a file with `half` mode active to queue it.",
                parse_mode="Markdown",
            )
            return
        await _flush_half_queue(update, context, chat_id, get_print_options(chat_id))
        return

    # Start from the options currently in force, as a copy — the live session
    # object must not be mutated until the whole message parses cleanly.
    opts = replace(get_print_options(chat_id), ts=time.monotonic())

    tokens = text.split()
    if not tokens or not parse_option_tokens(tokens, opts):
        await update.effective_message.reply_text(
            "❓ Unknown option. Use: `bw`, `color`, `1x`, `2x`, `3x`, `4x`, "
            "`a4`, `a5`, `half`, `normal`, `print`",
            parse_mode="Markdown",
        )
        return

    print_options[chat_id] = opts

    # If switching away from half mode, discard any pending queued files.
    notice = ""
    if opts.number_up != 2:
        cleared_count = discard_half_queue(chat_id)
        if cleared_count:
            notice = f"⚠️ {cleared_count} queued file(s) cleared (half mode disabled).\n"

    count = f"{opts.copies} copies" if opts.copies > 1 else "1 copy"
    format_str = f"{opts.media} (Half Sheet)" if opts.number_up == 2 else opts.media
    await update.effective_message.reply_text(
        f"{notice}⚙️ Settings updated: {opts.color_label}, {count}, {format_str}\n_(Active for 30m)_",
        parse_mode="Markdown",
    )


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
            ext_label = f"'{orig_ext}'" if orig_ext else "'(none)'"
            await msg.reply_text(
                f"❌ Unsupported file type {ext_label}. "
                f"Supported: {PRINTABLE_EXTENSIONS_DISPLAY}"
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


async def _print_and_reply(
    update: Update,
    chat_id: int,
    file_paths: list[str],
    opts: PrintOptions,
    success_text: str,
) -> None:
    """Print files, notify Home Assistant, reply, and always clean the files up.

    Shared by the normal and half-mode paths so their error handling and
    temporary-file cleanup cannot drift apart.
    """
    try:
        await print_file(file_paths, opts)

        # Fire the HA webhook after a confirmed print, before the Telegram reply
        await notify_homeassistant(
            file_name=", ".join(os.path.basename(fp) for fp in file_paths),
            chat_id=chat_id,
            copies=opts.copies,
            color=opts.color,
        )
        await update.effective_message.reply_text(success_text, parse_mode="Markdown")

    except RuntimeError as e:
        cmd_used = getattr(e, "cmd", None)
        if cmd_used:
            logger.error("Print failed: %s (command: %s)", e, cmd_used)
        else:
            logger.error("Print failed: %s", e)
        await update.effective_message.reply_text(f"❌ Print failed: {e}")

    except Exception as e:
        logger.exception("Unexpected error during print: %s", e)
        await update.effective_message.reply_text("❌ An unexpected error occurred while printing.")

    finally:
        for fp in file_paths:
            in_flight_paths.discard(fp)
            try:
                os.remove(fp)
                logger.info("Cleaned up %s", fp)
            except OSError as exc:
                if os.path.exists(fp):
                    logger.warning("Could not remove %s: %s", fp, exc)


async def _flush_half_queue(
    update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, opts: PrintOptions
) -> None:
    """Rate-limit, then print all files queued for this chat in half mode.

    Clears the queue on success.  Leaves the queue intact when rate-limited so
    the user can retry by sending `print` after the cooldown expires.
    """
    entry = get_half_queue(chat_id)
    if not entry or not entry.files:
        await update.effective_message.reply_text("❓ No files are queued for printing.")
        return

    # Files in the half queue were always queued in half mode; ensure opts reflect that
    # even if the 30-minute TTL has since expired and get_print_options returned defaults.
    if opts.number_up != 2:
        opts = replace(opts, number_up=2)

    # ── Rate limiting ────────────────────────────────────────────────────────
    remaining = cooldown_remaining(chat_id)
    if remaining:
        await update.effective_message.reply_text(
            f"⏳ Please wait {remaining}s before printing. "
            f"Your {len(entry.files)} queued file(s) are ready — send `print` when the cooldown ends.",
            parse_mode="Markdown",
        )
        return

    files = list(entry.files)
    in_flight_paths.update(files)
    half_queue.pop(chat_id, None)
    last_print_time[chat_id] = time.monotonic()

    file_count = len(files)
    sheet_count = (file_count + 1) // 2

    await _print_and_reply(
        update,
        chat_id,
        files,
        opts,
        f"✅ Sent {file_count} file(s) to printer! "
        f"(~{sheet_count} sheet{'s' if sheet_count != 1 else ''})"
        "\n_(Half mode still active — send your next file when ready.)_",
    )


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
    if opts.number_up == 2:
        queue = get_half_queue(chat_id)
        if queue and len(queue.files) >= MAX_HALF_QUEUE_FILES:
            await update.effective_message.reply_text(
                f"⚠️ Queue is full ({MAX_HALF_QUEUE_FILES} files maximum). "
                "Send `print` to print your queued files first.",
                parse_mode="Markdown",
            )
            return

        file_info = await _get_file_info(update)
        if file_info is None:
            return

        file_obj, orig_ext = file_info

        # Half mode prints through a merged PDF, so reject anything unmergeable
        # before downloading it rather than silently printing it full-page.
        if orig_ext not in MERGEABLE_EXTENSIONS:
            await update.effective_message.reply_text(
                f"❌ `{orig_ext}` cannot be combined in half mode "
                f"(supported: {MERGEABLE_EXTENSIONS_DISPLAY}).\n"
                "Send `normal` first to print it as a full page.",
                parse_mode="Markdown",
            )
            return

        os.makedirs(DATA_DIR, exist_ok=True)
        file_path = os.path.join(DATA_DIR, f"{uuid.uuid4().hex}{orig_ext}")
        in_flight_paths.add(file_path)
        try:
            await file_obj.download_to_drive(file_path)
        except BaseException:
            in_flight_paths.discard(file_path)
            try:
                os.remove(file_path)
            except OSError:
                pass
            raise

        logger.info("Half-mode: queued file at %s", file_path)

        entry = half_queue.setdefault(chat_id, HalfQueueEntry())
        entry.files.append(file_path)
        entry.ts = time.monotonic()
        in_flight_paths.discard(file_path)

        file_count = len(entry.files)
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
    remaining = cooldown_remaining(chat_id)
    if remaining:
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
    in_flight_paths.add(file_path)
    try:
        await file_obj.download_to_drive(file_path)
    except BaseException:
        in_flight_paths.discard(file_path)
        try:
            os.remove(file_path)
        except OSError:
            pass
        raise
    logger.info("File saved at %s", file_path)

    await _print_and_reply(update, chat_id, [file_path], opts, "✅ Sent to printer!")


# ---------------------------------------------------------------------------
# Core print logic
# ---------------------------------------------------------------------------

async def print_file(file_paths: list[str], opts: PrintOptions) -> None:
    """Send one or more files to the printer using lp.

    In half mode the inputs are merged into a single PDF first: passing several
    files to one lp job makes CUPS place them on separate sheets rather than
    applying the number-up layout.

    Always passes -h <CUPS_SERVER> and -d <PRINTER_NAME> explicitly.
    Both environment variables are required — raises RuntimeError if missing.
    """
    if not LP_BIN:
        raise RuntimeError("lp command not found — is cups-client installed?")

    if not file_paths:
        raise RuntimeError("Internal error: print_file called with empty file list")

    merged_path: str | None = None
    merge_proc = None
    lp_proc = None

    try:
        print_paths = list(file_paths)
        if opts.number_up > 1:
            unmergeable = [
                os.path.basename(path)
                for path in file_paths
                if os.path.splitext(path)[1].lower() not in MERGEABLE_EXTENSIONS
            ]
            if unmergeable:
                raise RuntimeError(
                    f"Half mode cannot combine: {', '.join(unmergeable)}. "
                    f"Supported: {MERGEABLE_EXTENSIONS_DISPLAY}"
                )
            merged_path = os.path.join(DATA_DIR, f"{uuid.uuid4().hex}_merged.pdf")
            in_flight_paths.add(merged_path)

            pad_for_half = "1" if len(file_paths) == 1 else "0"
            merge_proc = await asyncio.create_subprocess_exec(
                sys.executable,
                MERGE_SCRIPT,
                merged_path,
                pad_for_half,
                *file_paths,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                m_stdout, m_stderr = await asyncio.wait_for(merge_proc.communicate(), timeout=MERGE_TIMEOUT)
            except asyncio.TimeoutError:
                merge_proc.kill()
                await merge_proc.wait()
                raise RuntimeError("Merging took too long (over 60 s) — the file may be too complex.")
            except asyncio.CancelledError:
                merge_proc.kill()
                await merge_proc.wait()
                raise

            if merge_proc.returncode != 0:
                if merge_proc.returncode < 0:
                    raise RuntimeError("Merge was stopped (file too large or too complex).")
                err_str = m_stderr.decode().strip()[:MAX_STDERR_LENGTH]
                raise RuntimeError(err_str or "Merge failed.")

            print_paths = [merged_path]

        server = get_cups_server()
        printer = get_printer_name()

        # Build: lp -h <server> -d <printer> -o fit-to-page -o media=<media> [options] <file(s)>
        cmd = [LP_BIN, "-h", server, "-d", printer, "-o", "fit-to-page", "-o", f"media={opts.media}"]

        if opts.number_up > 1:
            cmd += ["-o", f"number-up={opts.number_up}"]
        if not opts.color:
            # ColorModel=Gray is standard CUPS; CNColorMode=mono is Canon UFRII specific
            cmd += ["-o", "ColorModel=Gray", "-o", "CNColorMode=mono"]
        if opts.copies > 1:
            cmd += ["-n", str(opts.copies)]
        cmd.extend(print_paths)

        cmd_str = " ".join(cmd)
        logger.info("Shell command: %s", cmd_str)

        lp_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(lp_proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            lp_proc.kill()
            await lp_proc.wait()
            ex = RuntimeError("lp command timed out after 30 seconds")
            ex.cmd = cmd_str  # type: ignore[attr-defined]
            raise ex
        except asyncio.CancelledError:
            lp_proc.kill()
            await lp_proc.wait()
            raise

        if lp_proc.returncode != 0:
            err_str = stderr.decode().strip()[:MAX_STDERR_LENGTH]
            logger.error(
                "lp failed (returncode=%s) stderr: %s",
                lp_proc.returncode,
                stderr.decode(),
            )
            ex = RuntimeError(err_str or "Print command failed")
            ex.cmd = cmd_str  # type: ignore[attr-defined]
            raise ex

        logger.info("lp stdout: %s", stdout.decode().strip())
    finally:
        if merged_path:
            in_flight_paths.discard(merged_path)
            try:
                os.remove(merged_path)
                logger.info("Cleaned up merged file %s", merged_path)
            except OSError as e:
                if os.path.exists(merged_path):
                    logger.warning("Could not remove merged file %s: %s", merged_path, e)


# ---------------------------------------------------------------------------
# Home Assistant integration
# ---------------------------------------------------------------------------

_ha_client: httpx.AsyncClient | None = None


def get_ha_client() -> httpx.AsyncClient:
    """Return a shared persistent AsyncClient for Home Assistant notifications."""
    global _ha_client
    if _ha_client is None or _ha_client.is_closed:
        _ha_client = httpx.AsyncClient(timeout=3)
    return _ha_client


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

    url = f"{ha_url}/api/events/notanext_job_sent"
    payload = {
        "file_name": file_name,
        "chat_id": chat_id,
        "copies": copies,
        "color": color,
    }
    try:
        client = get_ha_client()
        response = await client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {ha_token}"},
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
        BotCommand("preferences", "Set your default printing preferences"),
        BotCommand("status", "Check printer availability"),
        BotCommand("jobs", "Show the print queue"),
        BotCommand("cancel", "Cancel all jobs on this printer"),
        BotCommand("clean", "Delete cached files and clear queue"),
    ])

    # Start periodic cleanup task — track it so exceptions are not silently lost
    task = asyncio.create_task(cleanup_task())
    task.add_done_callback(_on_cleanup_task_done)
    logger.info("Periodic cleanup task started (6h interval)")


async def post_shutdown(application) -> None:
    """Close the shared Home Assistant HTTP client on a clean shutdown."""
    global _ha_client
    if _ha_client is not None and not _ha_client.is_closed:
        await _ha_client.aclose()
    _ha_client = None


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any exception a handler didn't catch, and tell the user something broke.

    Without this, PTB only logs unhandled exceptions — the user gets silence
    with no indication their message did anything at all. Best-effort: the
    reply itself is wrapped, since the exception may be a Telegram API error
    that would also break a plain reply (e.g. the chat was deleted).
    """
    logger.error("Unhandled exception while processing an update: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("❌ Something went wrong. Please try again.")
        except Exception:
            logger.warning("Could not notify the user about the error above.")


async def unsupported_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply to message types neither handler recognises (voice, video, stickers, ...).

    Without this, such messages match nothing and are silently dropped — the
    user has no way to tell whether the bot is even running.
    """
    await update.effective_message.reply_text(
        "❓ I can only print photos and documents. Use /help to see what's supported."
    )


def _on_cleanup_task_done(task: asyncio.Task) -> None:
    """Log if the background cleanup task crashes unexpectedly."""
    if not task.cancelled() and task.exception():
        logger.error("Cleanup task crashed: %s", task.exception())


async def cleanup_task() -> None:
    """Background task: evict stale print options and remove leftover data files."""
    while True:
        await asyncio.sleep(6 * 3600)  # every 6 hours
        now = time.monotonic()

        # Evict expired print_options entries that were never consumed (leak prevention)
        expired_chats = [
            cid for cid, entry in print_options.items()
            if (now - entry.ts) >= SESSION_TTL
        ]
        for cid in expired_chats:
            print_options.pop(cid, None)
        if expired_chats:
            logger.info("Evicted %d expired print option(s).", len(expired_chats))

        # Evict stale last_print_time entries (prevents unbounded growth)
        stale_rate = [
            cid for cid, ts in last_print_time.items()
            if (now - ts) >= SESSION_TTL
        ]
        for cid in stale_rate:
            last_print_time.pop(cid, None)
        if stale_rate:
            logger.info("Evicted %d stale rate-limit entry(ies).", len(stale_rate))

        # Evict expired half-queue entries and delete their files
        expired_half = [
            cid for cid, entry in half_queue.items()
            if (now - entry.ts) >= SESSION_TTL
        ]
        for cid in expired_half:
            entry = half_queue.pop(cid, None)
            if entry:
                for fp in entry.files:
                    try:
                        os.remove(fp)
                        logger.info("Evicted stale half-queue file: %s", fp)
                    except OSError:
                        pass
        if expired_half:
            logger.info("Evicted %d expired half-queue(s).", len(expired_half))

        # Remove leftover downloaded files, but protect files in active queue or in flight
        logger.info("Running periodic data cleanup...")
        try:
            active_files = {
                fp
                for entry in half_queue.values()
                for fp in entry.files
            }
            skip_set = frozenset(active_files | in_flight_paths)
            removed = await perform_cleanup_async(skip_set, min_age=SESSION_TTL)
            logger.info("Periodic cleanup removed %d file(s).", removed)
        except Exception as e:
            logger.error("Periodic cleanup failed: %s", e)


def perform_cleanup(skip_paths: frozenset[str] | None = None, min_age: float = 0) -> int:
    """Delete all cached files from the data directory. Returns count removed.

    skip_paths: Optional set of absolute file paths to preserve (e.g. active
    half-queue files that are still awaiting pairing or in-flight downloads).

    The persistent preferences file and any corrupt backup file are always
    preserved regardless of skip_paths.

    If min_age > 0, files whose mtime is newer than (time.time() - min_age) are kept.

    Synchronous — safe to call at startup before the event loop starts.
    Use perform_cleanup_async() from async contexts.
    """
    raw_skip = (set(skip_paths) if skip_paths else set()) | {PREFERENCES_FILE, CORRUPT_PREFERENCES_FILE}
    normalized_skip = {os.path.normpath(p) for p in raw_skip}
    now = time.time()
    cutoff = now - min_age if min_age > 0 else 0.0
    removed = 0
    try:
        with os.scandir(DATA_DIR) as entries:
            for entry in entries:
                if os.path.normpath(entry.path) in normalized_skip:
                    continue
                try:
                    if entry.is_file():
                        if min_age > 0 and entry.stat().st_mtime > cutoff:
                            continue
                        os.remove(entry.path)
                        removed += 1
                except OSError as e:
                    logger.error("Error removing %s: %s", entry.path, e)
    except FileNotFoundError:
        pass
    return removed


async def perform_cleanup_async(skip_paths: frozenset[str] | None = None, min_age: float = 0) -> int:
    """Async wrapper for perform_cleanup — offloads blocking I/O to a thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, perform_cleanup, skip_paths, min_age)


# ---------------------------------------------------------------------------
# Application builder & Entry point
# ---------------------------------------------------------------------------

async def stale_pref_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer callback queries for expired or unhandled preference buttons."""
    query = update.callback_query
    if query:
        await query.answer()
        try:
            await query.edit_message_text("This menu has expired — send /preferences to start again.")
        except Exception:
            pass


def build_application(token: str, allowed_chat_ids: list[int]) -> Application:
    """Build and configure the Telegram application with all handlers."""
    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_error_handler(error_handler)

    new_message = filters.UpdateType.MESSAGE
    if allowed_chat_ids:
        chat_id_filter = new_message & filters.Chat(chat_id=allowed_chat_ids)
    else:
        logger.warning(
            "ALLOWED_CHAT_IDS is not set — all Telegram users can print. "
            "Set this variable to restrict access."
        )
        chat_id_filter = new_message

    with warnings.catch_warnings():
        from telegram.warnings import PTBUserWarning

        warnings.filterwarnings(
            "ignore",
            message=r"If 'per_message=False', 'CallbackQueryHandler' will not be tracked.*",
            category=PTBUserWarning,
        )
        pref_conv = ConversationHandler(
            entry_points=[
                CommandHandler("start", start, filters=chat_id_filter),
                CommandHandler("preferences", preferences_command, filters=chat_id_filter),
            ],
            states={
                PREF_COLOR: [CallbackQueryHandler(pref_color_callback, pattern="^pref_color_")],
                PREF_MODE: [CallbackQueryHandler(pref_mode_callback, pattern="^pref_mode_")],
                PREF_PAPER: [CallbackQueryHandler(pref_paper_callback, pattern="^pref_paper_")],
            },
            fallbacks=[CommandHandler("cancel", cancel_preferences)],
            allow_reentry=True,
        )
    application.add_handler(pref_conv)
    application.add_handler(CallbackQueryHandler(stale_pref_button, pattern="^pref_"))

    application.add_handler(CommandHandler("help", help_command, filters=new_message))
    application.add_handler(CommandHandler("status", status, filters=new_message))

    # Restricted commands and message handlers
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
            handle_text_message,
        )
    )

    # Catch-all for message types none of the above match (voice, video,
    # stickers, animations, contacts, ...) — otherwise these are dropped
    # silently and the user has no idea the bot even saw them.
    application.add_handler(
        MessageHandler(
            chat_id_filter
            & (~filters.COMMAND)
            & (~filters.PHOTO)
            & (~filters.Document.ALL)
            & (~filters.TEXT),
            unsupported_message,
        )
    )

    return application


def main() -> None:
    token = os.getenv("TOKEN")
    if not token:
        logger.error("TOKEN environment variable is not set")
        raise SystemExit("TOKEN environment variable is required")

    allowed_chat_ids = get_allowed_chat_ids()

    # Log configuration at startup for easy debugging
    logger.info("NotaNext v%s starting...", VERSION)
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

    # Load persistent per-chat preferences (sync — before event loop starts)
    load_preferences()

    application = build_application(token, allowed_chat_ids)
    application.run_polling()


if __name__ == "__main__":
    main()
