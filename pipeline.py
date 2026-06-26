"""
pipeline.py
===========
Orchestrates extraction -> bank selection -> parsing -> validation for one PDF.

A bank is chosen up front (explicitly, or by auto-detection over the parser
:data:`bank_parsers.REGISTRY`) and its parser does the primary parse. The
positional column mapper (:class:`normalizer.StatementNormalizer`) remains a
SECONDARY fallback used only when the chosen parser yields nothing, so a viable
result is never discarded and we never return empty while one exists.

Pages are streamed one at a time (flat memory) with a "page X of Y" progress
callback, suitable for statements that run to hundreds of pages. Only the first
page is briefly buffered, to score the format before streaming the rest.

Encrypted PDFs: an optional *password* is forwarded to the extractor and is never
stored or logged; a wrong/missing password surfaces as
:class:`extractor.PasswordError` for the caller to handle (re-prompt).
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional, Tuple

from bank_parsers import REGISTRY, select_parser
from extractor import extract_pages
from line_parser import page_to_plines
from normalizer import StatementNormalizer
from validator import validate, validation_summary

_LOG = logging.getLogger(__name__)


def parse_pdf(
    pdf_path: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    collect_debug: bool = False,
    debug_line_cap: int = 100_000,
    password: str = "",
    bank: str = "auto",
) -> dict:
    """Parse *pdf_path* into a result dict.

    Returns keys: transactions, opening_balance, closing_balance, ocr_used,
    method, bank, bank_display, unrecognized, summary, debug_pages.
    """
    debug_pages: List[Tuple[int, list]] = []
    state = {"ocr_used": False, "debug_count": 0, "debug_truncated": False}

    def page_stream():
        """Yield pages while folding in OCR flags and the raw-debug dump."""
        for page in extract_pages(pdf_path, progress_callback=progress_callback,
                                  cancel_check=cancel_check, password=password):
            if getattr(page, "ocr_used", False):
                state["ocr_used"] = True
            if collect_debug and page.raw_debug and not state["debug_truncated"]:
                remaining = debug_line_cap - state["debug_count"]
                if remaining <= 0:
                    state["debug_truncated"] = True
                    debug_pages.append((0, [f"(Raw-extract dump truncated at {debug_line_cap:,} lines.)"]))
                else:
                    kept = page.raw_debug[:remaining]
                    debug_pages.append((page.page_num, kept))
                    state["debug_count"] += len(kept)
            yield page

    # --- choose the parser from the first page, then stream-parse the rest ---
    pages = page_stream()
    first = next(pages, None)                       # may raise PasswordError -> caller re-prompts
    if first is None:
        return _empty_result(bank, debug_pages, state["ocr_used"])

    detect_lines = [pl.text for pl in page_to_plines(first)]
    parser, unrecognized = select_parser(bank, detect_lines)

    def full_stream():
        yield first
        yield from pages

    out = parser.parse(full_stream())
    transactions = out.transactions
    opening_balance = out.opening_balance
    closing_balance = out.closing_balance
    method = parser.key

    # --- secondary fallback: positional column mapping (only if text gave nothing) ---
    if not transactions:
        _LOG.warning("Parser '%s' found no transactions; falling back to positional column mapping.",
                     parser.key)
        norm = StatementNormalizer()
        for page in extract_pages(pdf_path, cancel_check=cancel_check, password=password):
            norm.feed_page(page)
        transactions = norm.finalize()
        opening_balance = norm.opening_balance
        method = "positional-fallback"
        unrecognized = True

    validate(transactions, opening_balance)
    return {
        "transactions": transactions,
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "ocr_used": state["ocr_used"],
        "method": method,
        "bank": parser.key,
        "bank_display": parser.display_name,
        "unrecognized": unrecognized,
        "summary": validation_summary(transactions),
        "debug_pages": debug_pages,
    }


def _empty_result(bank: str, debug_pages: list, ocr_used: bool) -> dict:
    """A consistent result for a PDF that yielded no pages."""
    parser = REGISTRY.get(bank if bank and bank != "auto" else "generic", REGISTRY["generic"])
    return {
        "transactions": [],
        "opening_balance": None,
        "closing_balance": None,
        "ocr_used": ocr_used,
        "method": "empty",
        "bank": parser.key,
        "bank_display": parser.display_name,
        "unrecognized": True,
        "summary": validation_summary([]),
        "debug_pages": debug_pages,
    }
