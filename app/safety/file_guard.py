"""Upload guard: never trust the file name, the extension or the browser's content-type."""
import hashlib
import io
import os
import re
from dataclasses import dataclass

from app.safety import config

_MAGIC = {
    ".pdf": [b"%PDF-"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
}
# PDF features a medical bill never needs and attackers like
_PDF_RISKY = (b"/JavaScript", b"/JS", b"/OpenAction", b"/Launch", b"/EmbeddedFile", b"/AA", b"/RichMedia", b"/XFA")


@dataclass
class UploadVerdict:
    ok: bool
    reason: str = ""
    user_message: str = ""
    safe_name: str = ""
    ext: str = ""
    sha256: str = ""


def safe_filename(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    stem, ext = os.path.splitext(base)
    return (stem[:40] or "bill") + ext.lower()


def _reject(reason, msg):
    return UploadVerdict(False, reason=reason, user_message=msg)


def check_upload(filename: str, data: bytes) -> UploadVerdict:
    safe = safe_filename(filename)
    ext = os.path.splitext(safe)[1]

    if ext not in config.ALLOWED_EXTENSIONS:
        return _reject("extension", "Please upload your bill as a PDF, JPG, PNG or TXT file.")
    if not data:
        return _reject("empty", "That file is empty. Please upload your bill again.")
    if len(data) > config.MAX_UPLOAD_BYTES:
        return _reject("too_big", f"That file is too large. The limit is {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")

    # the real content must match the extension (a .pdf that is really an .exe is refused)
    if ext in _MAGIC:
        if not any(data.startswith(m) for m in _MAGIC[ext]):
            return _reject("magic_mismatch", "That file does not look like a real " + ext[1:].upper() + ". Please upload the original bill.")
    else:  # .txt
        if b"\x00" in data[:4096]:
            return _reject("binary_text", "That text file looks corrupted. Please upload your bill again.")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return _reject("not_utf8", "That text file could not be read. Please save it as UTF-8 or upload a PDF.")

    if ext == ".pdf":
        head = data[: 2_000_000]
        if b"/Encrypt" in head:
            return _reject("pdf_encrypted", "That PDF is password protected. Please upload an unlocked copy.")
        bad = [t.decode() for t in _PDF_RISKY if t in head]
        if bad:
            return _reject("pdf_active_content", "That PDF contains active content, so we cannot accept it. Please upload a plain bill.")
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                if len(pdf.pages) > config.MAX_PDF_PAGES:
                    return _reject("pdf_pages", f"Please upload a bill of at most {config.MAX_PDF_PAGES} pages.")
        except Exception:
            return _reject("pdf_corrupt", "That PDF could not be opened. Please upload the original bill.")

    if ext in (".png", ".jpg", ".jpeg"):
        try:
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = config.MAX_IMAGE_PIXELS            # decompression-bomb limit
            with Image.open(io.BytesIO(data)) as im:
                im.verify()
                if im.width * im.height > config.MAX_IMAGE_PIXELS:
                    return _reject("image_pixels", "That image is too large. Please upload a smaller photo of the bill.")
        except Exception:
            return _reject("image_corrupt", "That image could not be opened. Please upload the bill again.")

    return UploadVerdict(True, safe_name=safe, ext=ext, sha256=hashlib.sha256(data).hexdigest())
