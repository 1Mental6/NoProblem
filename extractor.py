"""
extractor.py
============
PDF -> raw positioned rows, streamed page-by-page so that very long statements
(hundreds of pages) never need to live in memory all at once.

The primary engine is **pdfplumber's word extraction with x/y coordinates**.
We deliberately do NOT trust text reading order for column assignment: every
word is kept with its (x0, x1, top) box so the normalizer can place values into
the right column by *horizontal position*. This is what keeps cheque numbers,
debits, credits and balances in their correct columns on statements whose text
stream is jumbled.

Per page, the extractor decides how to deliver the data:

    1. pdfplumber words -> grouped into physical lines (coordinate mode).
    2. camelot (lattice -> stream) -> pre-celled rows, used only when the
       coordinate result looks structurally empty (poorly aligned / failed).
    3. OCR (pytesseract + pdf2image) -> raw text lines, for scanned/image PDFs
       (flagged so the GUI can warn the user).

camelot / pytesseract / pdf2image are imported lazily and are optional: if they
are not installed the extractor simply skips that fallback.

It also emits, per page, a human-readable ``raw_debug`` dump (each word with its
coordinates) that the exporter writes to a "Debug — Raw Extract" sheet.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Iterator, List, NamedTuple, Optional

import pdfplumber

# --- optional binary locations (override via environment) --------------------
TESSERACT_CMD = os.environ.get("TESSERACT_CMD")     # full path to tesseract.exe
POPPLER_PATH = os.environ.get("POPPLER_PATH")       # folder with pdftoppm/pdfinfo
OCR_DPI = int(os.environ.get("OCR_DPI", "300"))

# A page with fewer than this many extractable characters is treated as scanned.
_SCANNED_TEXT_THRESHOLD = 10

# Cheap structure probes used only to decide the camelot fallback.
_DATE_TOKEN = re.compile(r"\d{1,2}[-/.][A-Za-z0-9]{2,9}[-/.]\d{2,4}")
_MONEY_TOKEN = re.compile(r"\d[\d,]*\.\d{2}")


class Word(NamedTuple):
    """A single positioned word as read from the PDF."""
    text: str
    x0: float
    x1: float
    top: float
    bottom: float


@dataclass
class PageData:
    """Everything the normalizer needs about one page.

    Exactly one of ``lines`` / ``rows`` / ``text_lines`` is populated depending
    on which engine handled the page.
    """
    page_num: int
    total_pages: int
    method: str
    lines: List[List[Word]] = field(default_factory=list)   # coordinate mode
    rows: List[List[str]] = field(default_factory=list)     # pre-celled (camelot)
    text_lines: List[str] = field(default_factory=list)     # OCR / plain text
    ocr_used: bool = False
    raw_debug: List[str] = field(default_factory=list)      # for the debug sheet


class ExtractionError(Exception):
    """Raised when a PDF cannot be opened / read at all."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def extract_pages(
    pdf_path: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Iterator[PageData]:
    """Yield :class:`PageData` for every page of *pdf_path*, one at a time."""
    try:
        pdf = pdfplumber.open(pdf_path)
    except Exception as exc:  # noqa: BLE001 - surface a clean message upstream
        raise ExtractionError(f"Could not open PDF: {exc}") from exc

    try:
        total = len(pdf.pages)
        for index, page in enumerate(pdf.pages, start=1):
            if cancel_check and cancel_check():
                break

            try:
                page_data = _extract_single_page(pdf_path, page, index, total)
            except Exception as exc:  # noqa: BLE001 - never let one page kill the run
                page_data = PageData(index, total, f"error: {exc}")

            try:
                page.flush_cache()
            except Exception:  # noqa: BLE001
                pass

            if progress_callback:
                progress_callback(index, total)

            yield page_data
    finally:
        pdf.close()


# ---------------------------------------------------------------------------
# Per-page extraction
# ---------------------------------------------------------------------------
def _extract_single_page(pdf_path: str, page, page_num: int, total: int) -> PageData:
    text = (page.extract_text() or "").strip()

    # 1. No extractable text -> scanned page -> OCR.
    if len(text) < _SCANNED_TEXT_THRESHOLD:
        lines = _ocr_page(pdf_path, page_num)
        if lines:
            return PageData(page_num, total, "ocr", text_lines=lines, ocr_used=True,
                            raw_debug=[f"[OCR] {ln}" for ln in lines])
        return PageData(page_num, total, "scanned-no-ocr")

    # 2. Digital page: extract positioned words and group into physical lines.
    words = _extract_words(page)
    word_lines = _group_lines(words)
    raw_debug = _format_debug(word_lines)

    if _structured_line_count(word_lines) >= 2:
        return PageData(page_num, total, "pdfplumber-coords",
                        lines=word_lines, raw_debug=raw_debug)

    # 3. Coordinate result looks structurally empty -> try camelot.
    rows, method = _camelot_tables(pdf_path, page_num)
    if rows:
        return PageData(page_num, total, method, rows=rows,
                        raw_debug=[" | ".join(r) for r in rows])

    # 4. Last resort: hand the joined text lines to the normalizer's line parser.
    text_lines = [" ".join(w.text for w in ln) for ln in word_lines] or text.splitlines()
    return PageData(page_num, total, "text-lines",
                    text_lines=[ln for ln in text_lines if ln.strip()],
                    raw_debug=raw_debug)


def _extract_words(page) -> List[Word]:
    """pdfplumber words -> list of :class:`Word` (text + box)."""
    try:
        raw = page.extract_words(
            x_tolerance=1.5, y_tolerance=3,
            keep_blank_chars=False, use_text_flow=False,
        )
    except Exception:  # noqa: BLE001
        raw = []

    out: List[Word] = []
    for w in raw:
        try:
            out.append(Word(str(w["text"]), float(w["x0"]), float(w["x1"]),
                            float(w["top"]), float(w["bottom"])))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _group_lines(words: List[Word], tol: float = 3.0) -> List[List[Word]]:
    """Cluster words into physical lines by their ``top`` coordinate.

    Each returned line is sorted left-to-right by ``x0``. A gap in ``top`` larger
    than *tol* starts a new line, so wrapped description lines become their own
    physical lines (which is exactly how multi-line merge later identifies them).
    """
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (w.top, w.x0))
    lines: List[List[Word]] = []
    current: List[Word] = [ordered[0]]
    prev_top = ordered[0].top
    for w in ordered[1:]:
        if w.top - prev_top > tol:
            lines.append(sorted(current, key=lambda x: x.x0))
            current = [w]
        else:
            current.append(w)
        prev_top = w.top
    lines.append(sorted(current, key=lambda x: x.x0))
    return lines


def _structured_line_count(lines: List[List[Word]]) -> int:
    """How many lines look like real statement rows (a date AND an amount)."""
    count = 0
    for ln in lines:
        txt = " ".join(w.text for w in ln)
        if _DATE_TOKEN.search(txt) and _MONEY_TOKEN.search(txt):
            count += 1
    return count


def _format_debug(lines: List[List[Word]]) -> List[str]:
    """Render each line as 'word@(x,y)' tokens for the debug sheet."""
    out: List[str] = []
    for ln in lines:
        out.append("  ".join(f"[x{w.x0:.0f},y{w.top:.0f}]{w.text}" for w in ln))
    return out


# ---------------------------------------------------------------------------
# camelot fallback (optional)
# ---------------------------------------------------------------------------
def _camelot_tables(pdf_path: str, page_num: int):
    """Try camelot lattice then stream. Returns ``(rows, method)``."""
    try:
        import camelot  # lazy, optional
    except Exception:  # noqa: BLE001 - not installed
        return [], "camelot-unavailable"

    for flavor in ("lattice", "stream"):
        try:
            tables = camelot.read_pdf(pdf_path, pages=str(page_num), flavor=flavor)
        except Exception:  # noqa: BLE001 - ghostscript missing, parse error, etc.
            continue

        rows: List[List[str]] = []
        for table in tables:
            for raw in table.df.values.tolist():
                cells = [str(c).replace("\n", " ").strip() for c in raw]
                if any(cells):
                    rows.append(cells)
        if len(rows) >= 2:
            return rows, f"camelot-{flavor}"

    return [], "camelot-empty"


# ---------------------------------------------------------------------------
# OCR fallback (optional)
# ---------------------------------------------------------------------------
def _ocr_page(pdf_path: str, page_num: int) -> List[str]:
    """OCR a single page -> list of text lines. Empty if OCR unavailable."""
    try:
        import pytesseract                       # lazy, optional
        from pdf2image import convert_from_path  # lazy, optional
    except Exception:  # noqa: BLE001 - not installed
        return []

    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

    try:
        kwargs = {"dpi": OCR_DPI, "first_page": page_num, "last_page": page_num}
        if POPPLER_PATH:
            kwargs["poppler_path"] = POPPLER_PATH
        images = convert_from_path(pdf_path, **kwargs)
    except Exception:  # noqa: BLE001 - poppler missing, etc.
        return []

    lines: List[str] = []
    for image in images:
        try:
            text = pytesseract.image_to_string(image)
        except Exception:  # noqa: BLE001 - tesseract missing
            return []
        lines.extend(ln for ln in text.splitlines() if ln.strip())
    return lines
