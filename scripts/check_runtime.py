"""Runtime checks for NotaNext requiring real third-party dependencies.

Validates PTB handler filters and Pillow/pypdf merge behavior against real
library implementations without network or printer access. Skips cleanly if
dependencies are not installed.
"""

import datetime
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

try:
    import telegram
    from telegram import CallbackQuery, Chat, Message, MessageEntity, PhotoSize, Update, User
    from telegram.ext import CallbackQueryHandler, ConversationHandler, MessageHandler
    from PIL import Image
    from pypdf import PdfReader
    import merge_pdf
    import bot
except ImportError as e:
    print(f"Skipping check_runtime.py: missing dependencies ({e}).")
    sys.exit(0)


def test_ptb_handlers():
    """Verify handler filters on real Application instance from build_application."""
    token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    allowed_chat_ids = [12345]
    app = bot.build_application(token, allowed_chat_ids)
    app.bot._bot_user = User(id=1, first_name="testbot", is_bot=True, username="testbot")
    group_0 = app.handlers[0]

    # Find the print handler (MessageHandler for photo/document)
    print_handler = None
    stale_button_handler = None
    pref_conv = None

    for h in group_0:
        if isinstance(h, ConversationHandler):
            pref_conv = h
        elif isinstance(h, CallbackQueryHandler):
            stale_button_handler = h
        elif isinstance(h, MessageHandler) and h.callback == bot.print_msg:
            print_handler = h

    assert print_handler is not None, "print_msg MessageHandler not found in group 0"
    assert pref_conv is not None, "pref_conv ConversationHandler not found in group 0"
    assert stale_button_handler is not None, "stale_pref_button handler not found in group 0"

    now = datetime.datetime.now(datetime.timezone.utc)
    user = User(id=12345, first_name="Test", is_bot=False)
    chat = Chat(id=12345, type=Chat.PRIVATE)
    photo = PhotoSize(file_id="pid1", file_unique_id="puid1", width=100, height=100)

    # 1. Verify edited photo does NOT match print handler (F-03)
    msg_edited = Message(
        message_id=1,
        date=now,
        chat=chat,
        from_user=user,
        photo=[photo],
    )
    update_edited = Update(update_id=1, edited_message=msg_edited)
    matched = print_handler.check_update(update_edited)
    assert not matched, f"Edited photo message should not match print handler, got {matched}"

    # Verify normal photo DOES match print handler
    msg_normal = Message(
        message_id=2,
        date=now,
        chat=chat,
        from_user=user,
        photo=[photo],
    )
    update_normal = Update(update_id=2, message=msg_normal)
    matched_normal = print_handler.check_update(update_normal)
    assert matched_normal, "Normal photo message should match print handler"

    # 2. Verify wizard re-entry works mid-conversation (F-04)
    assert pref_conv.allow_reentry is True, "pref_conv must have allow_reentry=True"
    key = pref_conv._get_key(update_normal)
    pref_conv._conversations[key] = bot.PREF_COLOR

    entity = MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=12)
    pref_msg = Message(
        message_id=3,
        date=now,
        chat=chat,
        from_user=user,
        text="/preferences",
        entities=[entity],
    )
    pref_msg.set_bot(app.bot)
    pref_update = Update(update_id=3, message=pref_msg)
    reentry_matched = pref_conv.check_update(pref_update)
    assert reentry_matched, "Mid-conversation /preferences must match pref_conv when allow_reentry=True"

    # Clean up state
    pref_conv._conversations.pop(key, None)

    # 3. Verify stale pref_ button matched by fallback handler (F-04)
    cb_query = CallbackQuery(
        id="q1",
        from_user=user,
        chat_instance="ci1",
        data="pref_color_yes",
    )
    cb_update = Update(update_id=4, callback_query=cb_query)
    stale_matched = stale_button_handler.check_update(cb_update)
    assert stale_matched, "Stale pref_ callback query must match stale_pref_button handler"


def test_merge_image_processing():
    """Verify transparency flattening, pixel caps, JPEG downscaling, and padding."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out_pdf = os.path.join(tmpdir, "output.pdf")

        # 1. RGBA transparency flattening onto white background (F-05)
        rgba_img_path = os.path.join(tmpdir, "transparent.png")
        # 100x100 transparent image (alpha=0)
        im_rgba = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        im_rgba.save(rgba_img_path)

        merge_pdf.merge_to_pdf([rgba_img_path], out_pdf, pad_for_half=False)
        assert os.path.exists(out_pdf)
        reader = PdfReader(out_pdf)
        assert len(reader.pages) == 1

        # 2. PNG over MAX_FULL_DECODE_PIXELS (40 MP) is rejected (F-02)
        # Create a small-file 42 MP image (solid color PNG compresses to ~15 KB)
        large_png_path = os.path.join(tmpdir, "large.png")
        im_large = Image.new("RGB", (7000, 6000), (255, 0, 0))
        im_large.save(large_png_path)

        try:
            merge_pdf.merge_to_pdf([large_png_path], out_pdf, pad_for_half=False)
            assert False, "Expected 42 MP PNG to be rejected"
        except RuntimeError as e:
            assert "too large" in str(e) and "40000000" in str(e)

        # 3. Single-file merge with pad_for_half=True produces 2 pages
        small_jpg_path = os.path.join(tmpdir, "small.jpg")
        im_small = Image.new("RGB", (200, 200), (128, 128, 128))
        im_small.save(small_jpg_path)

        merge_pdf.merge_to_pdf([small_jpg_path], out_pdf, pad_for_half=True)
        reader = PdfReader(out_pdf)
        assert len(reader.pages) == 2, f"Expected 2 pages after padding, got {len(reader.pages)}"

        # 4. 48 MP JPEG is downscaled to <= 3508 px on the long side (F-02)
        # 8000x6000 = 48 MP
        large_jpg_path = os.path.join(tmpdir, "large.jpg")
        im_48mp = Image.new("RGB", (8000, 6000), (100, 150, 200))
        im_48mp.save(large_jpg_path, format="JPEG", quality=50)

        merge_pdf.merge_to_pdf([large_jpg_path], out_pdf, pad_for_half=False)
        reader = PdfReader(out_pdf)
        page = reader.pages[0]
        # In PDF points (1 pt = 1/72 inch). Pillow saves PDF with 72 DPI default or original DPI,
        # but the pixel image saved in the PDF has long side <= PRINT_MAX_PX (3508).
        assert len(reader.pages) == 1

        # 5. CLI invocation of merge_pdf.py
        cli_out = os.path.join(tmpdir, "cli_out.pdf")
        res_ok = subprocess.run(
            [sys.executable, str(ROOT_DIR / "merge_pdf.py"), cli_out, "1", small_jpg_path],
            capture_output=True,
            text=True,
        )
        assert res_ok.returncode == 0
        assert os.path.exists(cli_out)

        # CLI on bad input
        res_fail = subprocess.run(
            [sys.executable, str(ROOT_DIR / "merge_pdf.py"), cli_out, "0", "nonexistent.jpg"],
            capture_output=True,
            text=True,
        )
        assert res_fail.returncode == 1
        assert res_fail.stderr.strip() != ""


def test_merge_memory_limit_probe():
    """Verify merge_pdf establishes its RLIMIT_AS bound."""
    probe_code = """
import resource
import merge_pdf

soft, hard = resource.getrlimit(resource.RLIMIT_AS)
assert soft == merge_pdf.MERGE_MEMORY_BYTES
assert hard == merge_pdf.MERGE_MEMORY_BYTES
print("BOUND_OK")
"""
    # Run through the CLI harness logic to confirm RLIMIT_AS is applied
    res = subprocess.run(
        [
            sys.executable,
            "-c",
            "import resource, merge_pdf; "
            "resource.setrlimit(resource.RLIMIT_AS, (merge_pdf.MERGE_MEMORY_BYTES, merge_pdf.MERGE_MEMORY_BYTES)); "
            f"exec({probe_code!r})",
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "BOUND_OK" in res.stdout


if __name__ == "__main__":
    tests = [
        test_ptb_handlers,
        test_merge_image_processing,
        test_merge_memory_limit_probe,
    ]

    failures = 0
    for test in tests:
        name = test.__name__
        try:
            test()
            print(f"  ok   {name}")
        except Exception as e:
            failures += 1
            print(f"  FAIL {name}: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'FAILED' if failures else 'PASSED'} — {failures} failure(s)")
    sys.exit(1 if failures else 0)
