"""Isolated PDF and image merging for NotaNext half-sheet mode.

Runs in a separate child process with memory limits (RLIMIT_AS) and a timeout to
isolate PDF/image decoding and prevent hangs or OOM crashes in the bot process.
Never imports bot, telegram, or httpx.
"""

import io
import math
import os
import resource
import sys
import warnings

MERGE_MEMORY_BYTES = 384 * 1024 * 1024  # 384 MiB virtual address space bound
MAX_MERGED_PAGES = 50
PRINT_MAX_PX = 3508  # Long side of A4 at 300 DPI
MAX_IMAGE_PIXELS = 120_000_000
MAX_FULL_DECODE_PIXELS = 40_000_000


def merge_to_pdf(file_paths: list[str], output_path: str, pad_for_half: bool = False) -> None:
    """Merge images and PDFs into a single monolithic PDF document.

    If pad_for_half is True and exactly one logical page is produced, append a
    blank page of the same size so CUPS number-up=2 reliably places the content
    on half of a physical sheet (instead of some drivers scaling full-page).
    """
    from PIL import Image, ImageOps
    from pypdf import PdfReader, PdfWriter

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)

    writer = PdfWriter()
    first_page_width: float | None = None
    first_page_height: float | None = None

    def _add_pages_from_reader(reader: PdfReader) -> None:
        nonlocal first_page_width, first_page_height
        for page in reader.pages:
            if len(writer.pages) >= MAX_MERGED_PAGES:
                raise RuntimeError(f"Merged document exceeds maximum of {MAX_MERGED_PAGES} pages.")
            if first_page_width is None or first_page_height is None:
                first_page_width = float(page.mediabox.width)
                first_page_height = float(page.mediabox.height)
            writer.add_page(page)

    for fp in file_paths:
        ext = os.path.splitext(fp)[1].lower()
        if ext in ('.jpg', '.jpeg', '.png', '.gif'):
            with Image.open(fp, formats=["JPEG", "PNG", "GIF"]) as img:
                w, h = img.size
                total_pixels = w * h
                if img.format in ("PNG", "GIF") and total_pixels > MAX_FULL_DECODE_PIXELS:
                    raise RuntimeError(
                        f"Image {os.path.basename(fp)} too large ({total_pixels} pixels). "
                        f"Maximum supported for {img.format} is {MAX_FULL_DECODE_PIXELS} pixels."
                    )
                if total_pixels > MAX_IMAGE_PIXELS:
                    raise RuntimeError(
                        f"Image {os.path.basename(fp)} too large ({total_pixels} pixels). "
                        f"Maximum supported is {MAX_IMAGE_PIXELS} pixels."
                    )

                # JPEG draft downscaling: request aspect-preserving size only if f < 1
                max_dim = max(w, h)
                f = PRINT_MAX_PX / max_dim if max_dim > 0 else 1.0
                if img.format == "JPEG" and f < 1.0:
                    img.draft("RGB", (math.ceil(w * f), math.ceil(h * f)))

                # Transpose EXIF orientation after draft
                img = ImageOps.exif_transpose(img)

                # Convert transparency to RGBA, otherwise RGB
                is_transparent = (img.mode in ("RGBA", "LA")) or ("transparency" in img.info)
                img = img.convert("RGBA") if is_transparent else img.convert("RGB")

                # Thumbnail to PRINT_MAX_PX before white canvas flattening
                img.thumbnail((PRINT_MAX_PX, PRINT_MAX_PX))

                # Composite RGBA onto white background
                if img.mode == "RGBA":
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    bg.paste(img, (0, 0), img)
                    img.close()
                    img = bg

                img_pdf = io.BytesIO()
                img.save(img_pdf, format="PDF")
                img.close()
                img_pdf.seek(0)
                _add_pages_from_reader(PdfReader(img_pdf))

        elif ext == '.pdf':
            # add_page() clones eagerly, so the handle can close once the loop ends.
            with open(fp, 'rb') as pdf_f:
                _add_pages_from_reader(PdfReader(pdf_f))
        else:
            raise RuntimeError(f"Half mode merging is only supported for Images and PDFs. Found: {ext}")

    if pad_for_half and len(writer.pages) == 1 and first_page_width and first_page_height:
        writer.add_blank_page(width=first_page_width, height=first_page_height)

    with open(output_path, "wb") as f:
        writer.write(f)


def main() -> None:
    if len(sys.argv) < 4:
        sys.stderr.write("Usage: merge_pdf.py <output> <pad 0|1> <input>...\n")
        sys.exit(1)

    output_path = sys.argv[1]
    pad_for_half = sys.argv[2] in ("1", "true", "True")
    inputs = sys.argv[3:]

    try:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (MERGE_MEMORY_BYTES, MERGE_MEMORY_BYTES))
        except Exception as e:
            raise RuntimeError(f"Failed to set memory limit: {e}") from e

        merge_to_pdf(inputs, output_path, pad_for_half=pad_for_half)
    except MemoryError:
        sys.stderr.write("Merge exceeded its memory limit (file too large or too complex).\n")
        sys.exit(1)
    except Exception as e:
        msg = str(e).strip() or "Merge failed."
        sys.stderr.write(f"{msg.splitlines()[0]}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
