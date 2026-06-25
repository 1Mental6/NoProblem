"""
pipeline.py
===========
Orchestrates extraction -> parsing -> validation for one PDF.

The single-line text parser (:mod:`line_parser`) is the PRIMARY path, because
header-based positional column mapping keeps failing across banks and producing
zero output. Positional mapping (:class:`normalizer.StatementNormalizer`) is only
attempted as a SECONDARY fallback when the line parser yields nothing — so a
viable text parse is never discarded and we never return an empty result while
one exists.

Pages are streamed one at a time (flat memory) with a "page X of Y" progress
callback, suitable for statements that run to hundreds of pages.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional, Tuple

from extractor import extract_pages
from line_parser import LineStatementParser, pline_from_words, PLine
from normalizer import StatementNormalizer
from validator import validate, validation_summary

_LOG = logging.getLogger(__name__)


def _page_to_plines(page) -> List[PLine]:
    """Convert one extractor PageData into positioned lines for the line parser."""
    if page.lines:                                   # coordinate words available
        return [pline_from_words(line) for line in page.lines if line]
    if page.text_lines:                              # OCR / plain text: synth y = index
        return [PLine(t, float(i)) for i, t in enumerate(page.text_lines) if t.strip()]
    return []


def parse_pdf(
    pdf_path: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    collect_debug: bool = False,
    debug_line_cap: int = 100_000,
) -> dict:
    """Parse *pdf_path* into a result dict.

    Returns keys: transactions, opening_balance, closing_balance, ocr_used,
    method, summary, debug_pages.
    """
    line_parser = LineStatementParser()
    ocr_used = False
    debug_pages: List[Tuple[int, list]] = []
    debug_count = 0
    debug_truncated = False

    # --- primary pass: streaming line parser ---
    for page in extract_pages(pdf_path, progress_callback=progress_callback,
                              cancel_check=cancel_check):
        if getattr(page, "ocr_used", False):
            ocr_used = True
        line_parser.feed_page(_page_to_plines(page))

        if collect_debug and page.raw_debug and not debug_truncated:
            remaining = debug_line_cap - debug_count
            if remaining <= 0:
                debug_truncated = True
                debug_pages.append((0, [f"(Raw-extract dump truncated at {debug_line_cap:,} lines.)"]))
            else:
                kept = page.raw_debug[:remaining]
                debug_pages.append((page.page_num, kept))
                debug_count += len(kept)

    transactions = line_parser.finalize()
    opening_balance = line_parser.opening_balance
    closing_balance = line_parser.closing_balance
    method = "line-parser"

    # --- secondary fallback: positional column mapping (only if text gave nothing) ---
    if not transactions:
        _LOG.warning("Line parser found no transactions; falling back to positional column mapping.")
        norm = StatementNormalizer()
        for page in extract_pages(pdf_path, cancel_check=cancel_check):
            norm.feed_page(page)
        transactions = norm.finalize()
        opening_balance = norm.opening_balance
        method = "positional-fallback"

    validate(transactions, opening_balance)
    return {
        "transactions": transactions,
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "ocr_used": ocr_used,
        "method": method,
        "summary": validation_summary(transactions),
        "debug_pages": debug_pages,
    }
