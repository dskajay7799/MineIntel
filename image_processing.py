"""
image_processing.py — Step 6: image input validation + OCR for the AI Assistant.

Workflow: Image -> OCR -> extracted text -> fed into the existing rag.py
SQL/evidence pipeline (see rag.answer_image_question). This module only
handles the "Image -> OCR -> text" part; it never talks to the database
and never decides what's true — it just extracts text, honestly, or
raises a clear error.

Images are processed in memory only and are never written to disk, per
the Step 6 spec ("do not store images permanently unless the existing
architecture requires it" — it doesn't).
"""

from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

ALLOWED_FORMATS = {"PNG", "JPEG"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB
MIN_IMAGE_BYTES = 16  # anything smaller can't possibly be a real image


def validate_and_load_image(file_bytes: bytes, declared_filename: str = "") -> Image.Image:
    """
    Validates the uploaded bytes are actually a real, supported image —
    never trusts the filename extension alone. Raises ValueError with a
    clear, user-facing message on any problem. Returns a loaded PIL Image
    on success.
    """
    if not file_bytes or len(file_bytes) < MIN_IMAGE_BYTES:
        raise ValueError("Empty or unreadable file uploaded.")

    if len(file_bytes) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image is too large ({len(file_bytes) / (1024 * 1024):.1f} MB). "
            f"Maximum allowed is {MAX_IMAGE_BYTES // (1024 * 1024)} MB."
        )

    # Verify integrity from actual bytes (magic-number/structure check),
    # not from the filename or Content-Type header.
    try:
        probe = Image.open(io.BytesIO(file_bytes))
        probe.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("File is not a valid image or is corrupted.") from exc

    # verify() invalidates the file handle — reopen for actual use.
    try:
        image = Image.open(io.BytesIO(file_bytes))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("File is not a valid image or is corrupted.") from exc

    if image.format not in ALLOWED_FORMATS:
        raise ValueError(
            f"Unsupported image format '{image.format or 'unknown'}'. "
            "Supported formats: PNG, JPG, JPEG."
        )

    return image


def extract_text(image: Image.Image) -> str:
    """
    Runs OCR on an already-validated image. Raises RuntimeError with a
    clear message on OCR failure — never returns fabricated text.
    """
    try:
        import pytesseract

        text = pytesseract.image_to_string(image)
        return text.strip()
    except Exception as exc:  # noqa: BLE001 — OCR engine failures, missing binary, etc.
        raise RuntimeError(f"OCR processing failed: {exc}") from exc
