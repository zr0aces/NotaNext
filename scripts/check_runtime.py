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
    from PIL import Image, ExifTags
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

    # 4. Verify edited /cancel does NOT match pref_conv fallback handler mid-conversation
    pref_conv._conversations[key] = bot.PREF_COLOR
    cancel_entity = MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=7)
    cancel_msg = Message(
        message_id=5,
        date=now,
        chat=chat,
        from_user=user,
        text="/cancel",
        entities=[cancel_entity],
    )
    cancel_msg.set_bot(app.bot)

    edited_cancel_update = Update(update_id=5, edited_message=cancel_msg)
    matched_edited_cancel = pref_conv.check_update(edited_cancel_update)
    assert not matched_edited_cancel, "Edited /cancel must not match pref_conv fallback handler"

    normal_cancel_update = Update(update_id=6, message=cancel_msg)
    matched_normal_cancel = pref_conv.check_update(normal_cancel_update)
    assert matched_normal_cancel, "Normal /cancel must match pref_conv fallback handler"
    pref_conv._conversations.pop(key, None)


def test_merge_image_processing():
    """Verify transparency flattening, pixel caps, JPEG downscaling, and padding."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out_pdf = os.path.join(tmpdir, "output.pdf")

        # 1. RGBA transparency flattening onto white background (F-05)
        # 100x100 image: left half transparent (0,0,0,0), right half opaque red (255,0,0,255)
        rgba_img_path = os.path.join(tmpdir, "transparent.png")
        im_rgba = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        for x in range(50, 100):
            for y in range(100):
                im_rgba.putpixel((x, y), (255, 0, 0, 255))
        im_rgba.save(rgba_img_path)

        merge_pdf.merge_to_pdf([rgba_img_path], out_pdf, pad_for_half=False)
        assert os.path.exists(out_pdf)
        reader = PdfReader(out_pdf)
        assert len(reader.pages) == 1
        img_extracted = reader.pages[0].images[0].image.convert("RGB")
        assert img_extracted.getpixel((10, 50)) == (255, 255, 255)
        r, g, b = img_extracted.getpixel((80, 50))
        assert r > 200 and g < 50 and b < 50

        # 2. Palette GIF transparency flattening onto white background
        gif_img_path = os.path.join(tmpdir, "transparent.gif")
        im_gif = Image.new("P", (100, 100), 0)
        palette = [0, 0, 0, 0, 0, 255] + [0] * 762
        im_gif.putpalette(palette)
        for x in range(50, 100):
            for y in range(100):
                im_gif.putpixel((x, y), 1)
        im_gif.save(gif_img_path, transparency=0)

        merge_pdf.merge_to_pdf([gif_img_path], out_pdf, pad_for_half=False)
        reader_gif = PdfReader(out_pdf)
        assert len(reader_gif.pages) == 1
        img_gif_extracted = reader_gif.pages[0].images[0].image.convert("RGB")
        assert img_gif_extracted.getpixel((10, 50)) == (255, 255, 255)
        r, g, b = img_gif_extracted.getpixel((80, 50))
        assert r < 50 and g < 50 and b > 200

        # 3. PNG over MAX_FULL_DECODE_PIXELS (12 MP) is rejected
        large_png_path = os.path.join(tmpdir, "large.png")
        im_large = Image.new("RGB", (7000, 2000), (255, 0, 0))  # 14 MP
        im_large.save(large_png_path)

        try:
            merge_pdf.merge_to_pdf([large_png_path], out_pdf, pad_for_half=False)
            assert False, "Expected 14 MP PNG to be rejected"
        except RuntimeError as e:
            assert "too large" in str(e) and str(merge_pdf.MAX_FULL_DECODE_PIXELS) in str(e)

        # 4. Single-file merge with pad_for_half=True produces 2 pages
        small_jpg_path = os.path.join(tmpdir, "small.jpg")
        im_small = Image.new("RGB", (200, 200), (128, 128, 128))
        im_small.save(small_jpg_path)

        merge_pdf.merge_to_pdf([small_jpg_path], out_pdf, pad_for_half=True)
        reader = PdfReader(out_pdf)
        assert len(reader.pages) == 2, f"Expected 2 pages after padding, got {len(reader.pages)}"

        # 5. 48 MP JPEG is downscaled to <= 3508 px on the long side (preserving aspect ratio)
        large_jpg_path = os.path.join(tmpdir, "large.jpg")
        im_48mp = Image.new("RGB", (8000, 6000), (100, 150, 200))
        im_48mp.save(large_jpg_path, format="JPEG", quality=50)

        merge_pdf.merge_to_pdf([large_jpg_path], out_pdf, pad_for_half=False)
        reader = PdfReader(out_pdf)
        assert len(reader.pages) == 1
        img_downscaled = reader.pages[0].images[0].image
        assert max(img_downscaled.size) <= merge_pdf.PRINT_MAX_PX
        assert img_downscaled.size == (3508, 2631)

        # 6. EXIF orientation fixture with distinct corner colors
        exif_jpg_path = os.path.join(tmpdir, "exif_corners.jpg")
        im_exif = Image.new("RGB", (400, 200), (0, 0, 0))
        # TL: Red, TR: Green, BL: Blue, BR: Yellow
        for x in range(30):
            for y in range(30):
                im_exif.putpixel((x, y), (255, 0, 0))
        for x in range(370, 400):
            for y in range(30):
                im_exif.putpixel((x, y), (0, 255, 0))
        for x in range(30):
            for y in range(170, 200):
                im_exif.putpixel((x, y), (0, 0, 255))
        for x in range(370, 400):
            for y in range(170, 200):
                im_exif.putpixel((x, y), (255, 255, 0))
        exif_data = im_exif.getexif()
        exif_data[0x0112] = 6  # Rotate 90 CW
        im_exif.save(exif_jpg_path, format="JPEG", quality=95, exif=exif_data)

        merge_pdf.merge_to_pdf([exif_jpg_path], out_pdf, pad_for_half=False)
        reader_exif = PdfReader(out_pdf)
        img_oriented = reader_exif.pages[0].images[0].image.convert("RGB")
        assert img_oriented.size == (200, 400)
        # TL: Blue, TR: Red, BL: Yellow, BR: Green
        assert img_oriented.getpixel((10, 10))[2] > 200  # Blue
        assert img_oriented.getpixel((190, 10))[0] > 200  # Red
        assert img_oriented.getpixel((10, 390))[0] > 200 and img_oriented.getpixel((10, 390))[1] > 200  # Yellow
        assert img_oriented.getpixel((190, 390))[1] > 200  # Green

        # 7. CLI invocation of merge_pdf.py
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
    """Verify merge_pdf establishes its RLIMIT_AS bound using real main()."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out_txt = os.path.join(tmpdir, "limits.txt")
        in_dummy = os.path.join(tmpdir, "dummy.jpg")
        Path(in_dummy).write_bytes(b"dummy")

        script = f"""
import sys, resource, merge_pdf

def mock_merge(input_files, output_pdf, pad_for_half=False):
    limits = resource.getrlimit(resource.RLIMIT_AS)
    with open({out_txt!r}, "w") as f:
        f.write(f"{{limits[0]}},{{limits[1]}}")

merge_pdf.merge_to_pdf = mock_merge
sys.argv = ["merge_pdf.py", {out_txt!r}, "0", {in_dummy!r}]
merge_pdf.main()
"""
        res = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert res.returncode == 0, f"Probe failed: {res.stderr}"
        assert os.path.exists(out_txt)
        soft, hard = [int(x) for x in Path(out_txt).read_text().split(",")]
        assert soft == merge_pdf.MERGE_MEMORY_BYTES
        assert hard == merge_pdf.MERGE_MEMORY_BYTES


def test_admission_boundary_fixtures():
    """Verify boundary fixtures pass under 384 MiB bound and over-caps fail cleanly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cli_py = str(ROOT_DIR / "merge_pdf.py")

        # 1. 8000x6000 noise JPEG (48 MP, q95) -> exit 0
        im_noise8k = Image.effect_noise((8000, 6000), 50).convert("RGB")
        p_8k = os.path.join(tmpdir, "noise8k.jpg")
        im_noise8k.save(p_8k, "JPEG", quality=95)
        r = subprocess.run(
            [sys.executable, cli_py, os.path.join(tmpdir, "out_8k.pdf"), "0", p_8k],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, f"8000x6000 noise JPEG failed: code {r.returncode}, {r.stderr}"

        # 2. 6928x6928 constant-color JPEG -> exit 0
        p_const = os.path.join(tmpdir, "const6928.jpg")
        Image.new("RGB", (6928, 6928), (120, 150, 180)).save(p_const, "JPEG", quality=95)
        r = subprocess.run(
            [sys.executable, cli_py, os.path.join(tmpdir, "out_const.pdf"), "0", p_const],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, f"6928x6928 const JPEG failed: code {r.returncode}, {r.stderr}"

        # 3. 6928x6928 noise JPEG (q95) -> exit 0
        p_noise = os.path.join(tmpdir, "noise6928.jpg")
        im_noise6928 = Image.effect_noise((6928, 6928), 50).convert("RGB")
        im_noise6928.save(p_noise, "JPEG", quality=95)
        r = subprocess.run(
            [sys.executable, cli_py, os.path.join(tmpdir, "out_noise.pdf"), "0", p_noise],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, f"6928x6928 noise JPEG failed: code {r.returncode}, {r.stderr}"

        # 4. 6928x6928 EXIF 6 JPEG -> exit 0
        p_exif = os.path.join(tmpdir, "exif6928.jpg")
        exif = im_noise6928.getexif()
        exif[0x0112] = 6
        im_noise6928.save(p_exif, "JPEG", quality=95, exif=exif)
        r = subprocess.run(
            [sys.executable, cli_py, os.path.join(tmpdir, "out_exif.pdf"), "0", p_exif],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, f"6928x6928 EXIF 6 JPEG failed: code {r.returncode}, {r.stderr}"

        # 5. 6928x6928 CMYK JPEG -> exit 0
        p_cmyk = os.path.join(tmpdir, "cmyk6928.jpg")
        Image.new("CMYK", (6928, 6928), (100, 50, 0, 20)).save(p_cmyk, "JPEG", quality=95)
        r = subprocess.run(
            [sys.executable, cli_py, os.path.join(tmpdir, "out_cmyk.pdf"), "0", p_cmyk],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, f"6928x6928 CMYK JPEG failed: code {r.returncode}, {r.stderr}"

        # 6. 4000x3000 RGBA PNG (transparent background) -> exit 0
        p_png = os.path.join(tmpdir, "trans4k.png")
        Image.new("RGBA", (4000, 3000), (0, 0, 0, 0)).save(p_png)
        r = subprocess.run(
            [sys.executable, cli_py, os.path.join(tmpdir, "out_png.pdf"), "0", p_png],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, f"4000x3000 RGBA PNG failed: code {r.returncode}, {r.stderr}"

        # 7. 4000x3000 GIF (transparency) -> exit 0
        p_gif = os.path.join(tmpdir, "trans4k.gif")
        im_gif = Image.new("P", (4000, 3000), 0)
        im_gif.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
        im_gif.save(p_gif, transparency=0)
        r = subprocess.run(
            [sys.executable, cli_py, os.path.join(tmpdir, "out_gif.pdf"), "0", p_gif],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, f"4000x3000 GIF failed: code {r.returncode}, {r.stderr}"

        # 8. 8001x6000 JPEG -> exit 1, stderr names pixel cap
        p_over_jpg = os.path.join(tmpdir, "over.jpg")
        Image.new("RGB", (8001, 6000), (100, 100, 100)).save(p_over_jpg, "JPEG")
        r = subprocess.run(
            [sys.executable, cli_py, os.path.join(tmpdir, "out_over_jpg.pdf"), "0", p_over_jpg],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 1
        assert str(merge_pdf.MAX_IMAGE_PIXELS) in r.stderr

        # 9. 4001x3000 PNG -> exit 1, stderr names pixel cap
        p_over_png = os.path.join(tmpdir, "over.png")
        Image.new("RGBA", (4001, 3000), (100, 100, 100, 255)).save(p_over_png)
        r = subprocess.run(
            [sys.executable, cli_py, os.path.join(tmpdir, "out_over_png.pdf"), "0", p_over_png],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 1
        assert str(merge_pdf.MAX_FULL_DECODE_PIXELS) in r.stderr


def test_allocation_exhaustion_clean_reason():
    """Verify allocation failure under RLIMIT_AS gives a clean reason and exit 1."""
    with tempfile.TemporaryDirectory() as tmpdir:
        marker_file = os.path.join(tmpdir, "marker.txt")
        out_pdf = os.path.join(tmpdir, "out.pdf")
        in_dummy = os.path.join(tmpdir, "in.jpg")
        Path(in_dummy).write_bytes(b"dummy")

        script = f"""
import os, sys
sys.path.insert(0, {str(ROOT_DIR)!r})
import merge_pdf, PIL, pypdf

vmsize = 0
with open('/proc/self/status') as f:
    for line in f:
        if line.startswith('VmSize:'):
            vmsize = int(line.split()[1]) * 1024
            break

limit = vmsize + 32 * 1024 * 1024
merge_pdf.MERGE_MEMORY_BYTES = limit

def fake_merge(inputs, output_pdf, pad_for_half=False):
    with open({marker_file!r}, 'w') as f:
        f.write('REACHED')
    b = bytearray(limit + 16 * 1024 * 1024)
    b[0] = 1

merge_pdf.merge_to_pdf = fake_merge
sys.argv = ['merge_pdf.py', {out_pdf!r}, '0', {in_dummy!r}]
merge_pdf.main()
"""
        res = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert res.returncode == 1, f"Expected returncode 1, got {res.returncode}"
        assert os.path.exists(marker_file), "Expected marker file to be created"
        stderr_lines = res.stderr.strip().splitlines()
        assert len(stderr_lines) == 1, f"Expected single stderr line, got {len(stderr_lines)}: {res.stderr}"
        assert "memory limit" in stderr_lines[0], f"Expected 'memory limit' in stderr, got: {stderr_lines[0]}"
        assert "Traceback" not in res.stderr, f"Unexpected traceback in stderr: {res.stderr}"


if __name__ == "__main__":
    tests = [
        test_ptb_handlers,
        test_merge_image_processing,
        test_merge_memory_limit_probe,
        test_admission_boundary_fixtures,
        test_allocation_exhaustion_clean_reason,
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
