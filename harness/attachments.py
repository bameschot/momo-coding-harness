"""File attachments: turn an uploaded file into plain text for the model.

Text-based files (source, JSON, CSV, XML, Markdown, logs, ...) are decoded as
text; PDFs are converted with pypdf (optional dependency).  Attachments travel
inside the user message as <attachment> blocks so the model sees the content,
while frontends show a compact one-line summary instead of the whole file.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass

MAX_UPLOAD_BYTES = 25_000_000   # raw upload size (PDFs can be large)
MAX_TEXT_CHARS   = 1_000_000    # extracted text kept per file

_ATTACH_OPEN = re.compile(r'<attachment name="([^"]*)" chars="(\d+)">\n')
_ATTACH_CLOSE = "\n</attachment>"


# pypdf reports font/encoding quirks through `logging`. With no handler those land
# on stderr, which would scribble over the curses TUI — swallow them here.
_pypdf_log = logging.getLogger("pypdf")
_pypdf_log.addHandler(logging.NullHandler())
_pypdf_log.propagate = False


class AttachmentError(ValueError):
    """The file cannot be turned into text (binary, empty PDF, ...)."""


@dataclass
class Extracted:
    name: str
    text: str
    kind: str              # "text" | "pdf"
    pages: int | None = None
    truncated: bool = False


def extract(name: str, data: bytes) -> Extracted:
    """Convert raw file bytes to text, or raise AttachmentError."""
    if not data:
        raise AttachmentError(f"{name} is empty")
    if name.lower().endswith(".pdf") or data[:5] == b"%PDF-":
        text, pages = _pdf_text(name, data)
        kind = "pdf"
    else:
        text, pages = _decode_text(name, data), None
        kind = "text"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        text = text[:MAX_TEXT_CHARS] + f"\n... (truncated after {MAX_TEXT_CHARS:,} characters)"
    return Extracted(name=name, text=text, kind=kind, pages=pages, truncated=truncated)


def _decode_text(name: str, data: bytes) -> str:
    # BOM-marked UTF-16/32 first; then UTF-8; then reject anything binary.
    for bom, enc in ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                     (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            return data.decode(enc, errors="replace")
    if b"\x00" in data[:8192]:
        raise AttachmentError(f"{name} looks like a binary file — only text-based files and PDFs are supported")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _pdf_text(name: str, data: bytes) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise AttachmentError("PDF support needs the 'pypdf' package: pip install pypdf") from None
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")  # many "protected" PDFs only restrict editing
            except Exception:
                raise AttachmentError(f"{name} is password-protected") from None
        parts = []
        for i, page in enumerate(reader.pages, 1):
            page_text = (page.extract_text() or "").strip()
            if page_text:
                parts.append(f"[page {i}]\n{page_text}")
    except AttachmentError:
        raise
    except Exception as e:
        raise AttachmentError(f"could not read {name} as a PDF: {e}") from None
    if not parts:
        raise AttachmentError(f"{name} contains no extractable text (it may be a scanned image)")
    return "\n\n".join(parts), len(reader.pages)


def _attr(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def compose(text: str, attachments: list[dict]) -> str:
    """Build the model-facing message: typed text followed by attachment blocks."""
    blocks = [text.strip()] if text.strip() else []
    for att in attachments:
        body = str(att.get("text") or "")
        blocks.append(f'<attachment name="{_attr(str(att.get("name") or "file"))}" '
                      f'chars="{len(body)}">\n{body}\n</attachment>')
    return "\n\n".join(blocks)


def _parse(text: str) -> list[str | dict]:
    """A stored message as its pieces: typed text (str) and attachment blocks
    ({name, text}).  The block's chars="N" gives the exact body length, so file
    content that itself contains '</attachment>' cannot end a block early; an
    ill-formed block is left as text."""
    pieces: list[str | dict] = []
    pos = 0
    while (m := _ATTACH_OPEN.search(text, pos)):
        end = m.end() + int(m.group(2))
        if not text.startswith(_ATTACH_CLOSE, end):
            pieces.append(text[pos:m.end()])
            pos = m.end()
            continue
        pieces.append(text[pos:m.start()])
        name = m.group(1).replace("&quot;", '"').replace("&lt;", "<").replace("&amp;", "&")
        pieces.append({"name": name, "text": text[m.end():end]})
        pos = end + len(_ATTACH_CLOSE)
    pieces.append(text[pos:])
    return pieces


def split(text: str) -> tuple[str, list[dict]]:
    """Inverse of compose(): (typed text, [{name, text}]) from a stored message."""
    pieces = _parse(text)
    typed = "".join(p for p in pieces if isinstance(p, str))
    return typed.strip(), [p for p in pieces if isinstance(p, dict)]


def summarize(text: str) -> str:
    """Replace attachment blocks with a one-line '📎 name (N chars)' for display."""
    return "".join(p if isinstance(p, str) else f"📎 {p['name']} ({len(p['text']):,} chars)"
                   for p in _parse(text))
