"""
bank_parsers.py
===============
Registry of bank-specific statement parsers.

Each bank format is a small, self-contained parser class exposing two methods:

    detect(text_lines) -> float          # confidence in [0, 1] that this is "my"
                                         # format, from cheap raw-text signatures
    parse(pages)       -> ParseOutput    # actually extract the transactions

``detect`` is given the plain text lines of (the first page of) a statement so it
can score a layout without any heavy work. ``parse`` is given the *positioned*
pages (``extractor.PageData`` — words with x/y coordinates), because real
extraction needs geometry (description wrapping, split-line association, column
edges). ``parse`` consumes ``pages`` lazily, so very long statements still stream
one page at a time.

All parsers are registered in :data:`REGISTRY`, keyed by a short bank key. Adding
a new bank is therefore a matter of writing ONE class and adding it to
:data:`_PARSER_CLASSES` — nothing else changes. See :class:`ExampleBankParser`
at the bottom for a copy-paste template.

Selection
---------
* A specific bank chosen in the GUI/CLI -> that parser is used directly, skipping
  auto-detection (see :func:`select_parser`).
* "Auto-detect" -> every implemented parser scores the statement; the highest
  scorer above :data:`CONFIDENCE_THRESHOLD` wins. If none is confident, the
  :class:`GenericParser` best-effort line parser runs and the result is flagged
  ``unrecognized`` so the GUI can warn the user to review it.

The three production parsers (Indian Bank, SBI, HDFC) wrap the existing,
battle-tested :class:`line_parser.LineStatementParser` engine — pinned to the
right internal path via ``force_format`` — so this refactor adds structure
without changing any proven parsing behaviour. Stub parsers for banks not yet
tuned (ICICI, Union Bank, Canara Bank, IDBI, Bandhan Bank) are registered but
delegate to the generic parser until real samples are analysed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from bandhan_parser import BandhanStatementParser, detect_confidence as _bandhan_confidence
from canara_parser import CanaraStatementParser, detect_confidence as _canara_confidence
from idbi_parser import IDBIStatementParser, detect_confidence as _idbi_confidence
from extractor import PageData
from line_parser import (
    LineStatementParser, page_to_plines, _HDFC_MONEY, _LEAD_DATE,
)

_LOG = logging.getLogger(__name__)

# A statement scoring below this in auto-detect is treated as "format unknown"
# and handed to the generic best-effort parser (flagged for review).
CONFIDENCE_THRESHOLD = 0.5

# A balance token with a glued Cr/Dr suffix, e.g. "119544.23Cr" -- the Indian
# Bank signature (single amount column, direction carried as a suffix).
_CRDR_SUFFIX = re.compile(r"^[₹]?[\d,]+\.\d{1,2}(cr|dr)$", re.I)


@dataclass
class ParseOutput:
    """What every parser returns: the rows plus the statement-level balances."""
    transactions: List[dict]
    opening_balance: Optional[float]
    closing_balance: Optional[float]


# ===========================================================================
# Raw-text signature helpers (cheap; used only by detect())
# ===========================================================================
def _strip_leading_dates(text: str) -> Optional[str]:
    """Return the text after one or two leading dates, or None if not dated."""
    m1 = _LEAD_DATE.match(text)
    if not m1:
        return None
    rest = text[m1.end():].lstrip()
    m2 = _LEAD_DATE.match(rest)
    if m2:
        rest = rest[m2.end():].lstrip()
    return rest


def _is_hdfc_line(text: str) -> bool:
    """HDFC/Axis: a standalone CR|DR sitting between two money amounts."""
    return _HDFC_MONEY.search(text) is not None


def _is_sbi_line(text: str) -> bool:
    """SBI: a dated line whose trailing Ref/Debit/Credit/Balance block uses dashes."""
    rest = _strip_leading_dates(text)
    if rest is None:
        return False
    return LineStatementParser._parse_dash_tail(rest) is not None


def _is_indian_bank_line(text: str) -> bool:
    """Indian Bank: a dated line ending in a balance with a glued Cr/Dr suffix."""
    rest = _strip_leading_dates(text)
    if rest is None:
        return False
    toks = rest.split()
    return bool(toks) and _CRDR_SUFFIX.match(toks[-1]) is not None


def _score(lines: Iterable[str], predicate, ceiling: float) -> float:
    """Confidence from how many lines match *predicate* (saturating at ceiling)."""
    hits = sum(1 for ln in lines if predicate(ln))
    if not hits:
        return 0.0
    return min(ceiling, 0.6 + 0.1 * hits)


# ===========================================================================
# Base parser
# ===========================================================================
class BankParser:
    """Base class. Subclass, set ``key``/``display_name``, override the methods."""

    key: str = "base"
    display_name: str = "Base"
    implemented: bool = True            # False for not-yet-tuned stubs

    def detect(self, text_lines: List[str]) -> float:
        """Confidence in [0, 1] that *text_lines* are this bank's format."""
        return 0.0

    def make_engine(self) -> LineStatementParser:
        """Build the streaming engine this parser drives (one per parse)."""
        return LineStatementParser()

    def parse(self, pages: Iterable[PageData]) -> ParseOutput:
        """Stream *pages* through the engine and collect the transactions."""
        engine = self.make_engine()
        for page in pages:
            engine.feed_page(page_to_plines(page))
        return ParseOutput(engine.finalize(), engine.opening_balance, engine.closing_balance)


# ===========================================================================
# Production parsers — wrap the existing LineStatementParser engine
# ===========================================================================
class IndianBankParser(BankParser):
    """Single amount column, direction as a glued ``Cr``/``Dr`` balance suffix."""
    key = "indian_bank"
    display_name = "Indian Bank"

    def detect(self, text_lines: List[str]) -> float:
        return _score(text_lines, _is_indian_bank_line, ceiling=0.90)

    def make_engine(self) -> LineStatementParser:
        return LineStatementParser(force_format="line")


class SBIParser(BankParser):
    """Dash-delimited Ref | Debit | Credit | Balance columns (empty cell = ``-``)."""
    key = "sbi"
    display_name = "SBI"

    def detect(self, text_lines: List[str]) -> float:
        return _score(text_lines, _is_sbi_line, ceiling=0.95)

    def make_engine(self) -> LineStatementParser:
        return LineStatementParser(force_format="line")


class HDFCParser(BankParser):
    """Single Amount column with a separate CR|DR indicator between amt & balance."""
    key = "hdfc"
    display_name = "HDFC"

    def detect(self, text_lines: List[str]) -> float:
        return _score(text_lines, _is_hdfc_line, ceiling=0.97)

    def make_engine(self) -> LineStatementParser:
        return LineStatementParser(force_format="hdfc")


class BandhanBankParser(BankParser):
    """Dates split across two lines (DD-MMM- on line A, YYYY on line B); separate
    Debit/Credit columns that show 0.00 when empty. Has its own engine."""
    key = "bandhan"
    display_name = "Bandhan Bank"

    def detect(self, text_lines: List[str]) -> float:
        return _bandhan_confidence(text_lines)

    def make_engine(self) -> BandhanStatementParser:
        return BandhanStatementParser()


class CanaraBankParser(BankParser):
    """Complete DD-MMM-YY dates, a Branch + optional Ref column, and positional
    Withdraws/Deposit/Balance money columns (negative balances). Own engine."""
    key = "canara"
    display_name = "Canara Bank"

    def detect(self, text_lines: List[str]) -> float:
        return _canara_confidence(text_lines)

    def make_engine(self) -> CanaraStatementParser:
        return CanaraStatementParser()


class IDBIBankParser(BankParser):
    """Fixed-width REP31 ledger: empty Debit/Credit column vanishes, leaving only
    amount + Cr/Dr balance, so direction comes from the balance movement. Own engine."""
    key = "idbi"
    display_name = "IDBI Bank"

    def detect(self, text_lines: List[str]) -> float:
        return _idbi_confidence(text_lines)

    def make_engine(self) -> IDBIStatementParser:
        return IDBIStatementParser()


class GenericParser(BankParser):
    """Best-effort fallback: leading-date + money-token line parsing (full auto).

    Used when auto-detection is not confident, or as the delegate for stub banks.
    Results are flagged ``unrecognized`` so the user reviews them carefully.
    """
    key = "generic"
    display_name = "Auto-detect (generic fallback)"

    def detect(self, text_lines: List[str]) -> float:
        return 0.10            # baseline only; never beats a real signature

    def make_engine(self) -> LineStatementParser:
        return LineStatementParser(force_format=None)


# ===========================================================================
# Stub parsers — registered but not yet tuned; delegate to the generic parser
# ===========================================================================
class _StubParser(BankParser):
    """A placeholder for a bank whose real format has not been analysed yet."""
    implemented = False

    def detect(self, text_lines: List[str]) -> float:
        return 0.0             # cannot recognise -> never auto-selected

    def make_engine(self) -> LineStatementParser:
        _LOG.warning("format not yet tuned for %s; using generic best-effort parser",
                     self.display_name)
        return LineStatementParser(force_format=None)


class ICICIParser(_StubParser):
    key = "icici"
    display_name = "ICICI"


class UnionBankParser(_StubParser):
    key = "union"
    display_name = "Union Bank"


# (Bandhan Bank, Canara Bank and IDBI are now fully implemented parsers above.)


# ===========================================================================
# Registry + selection
# ===========================================================================
# Order here is the order banks appear in the GUI dropdown (after "Auto-detect").
_PARSER_CLASSES = [
    HDFCParser,
    ICICIParser,
    UnionBankParser,
    CanaraBankParser,
    IDBIBankParser,
    BandhanBankParser,
    IndianBankParser,
    SBIParser,
    GenericParser,            # always last; the fallback, not shown as a choice
]

REGISTRY: Dict[str, BankParser] = {cls.key: cls() for cls in _PARSER_CLASSES}

# (key, label) pairs for the GUI dropdown: "Auto-detect" first, generic hidden.
BANK_CHOICES = [("auto", "Auto-detect")] + [
    (cls.key, cls().display_name) for cls in _PARSER_CLASSES if cls is not GenericParser
]


def select_parser(bank_key: Optional[str], text_lines: List[str]):
    """Resolve ``(parser, unrecognized)`` for a bank choice and statement text.

    * A concrete bank key -> that parser (``unrecognized`` True only for stubs).
    * ``"auto"`` / None    -> the most confident parser above the threshold, else
      the generic fallback with ``unrecognized=True``.
    """
    if bank_key and bank_key not in ("auto", None):
        parser = REGISTRY.get(bank_key, REGISTRY["generic"])
        return parser, (not parser.implemented)

    scored = [
        (p.detect(text_lines), p)
        for p in REGISTRY.values()
        if p.implemented and p.key != "generic"
    ]
    scored.sort(key=lambda s: s[0], reverse=True)
    if scored and scored[0][0] >= CONFIDENCE_THRESHOLD:
        return scored[0][1], False
    return REGISTRY["generic"], True


# ===========================================================================
# Template for adding a new bank (copy, rename, implement, register)
# ===========================================================================
class ExampleBankParser(BankParser):
    """TEMPLATE — copy this to add a real bank parser.

    Steps to add "Foo Bank":
        1. Copy this class, rename it ``FooBankParser``.
        2. Set ``key`` (short, unique) and ``display_name``.
        3. In ``detect`` return a high score ONLY for Foo Bank's unique line
           signature (a token pattern no other bank produces), else 0.0.
        4. In ``make_engine`` return the engine that parses it. If Foo Bank fits
           one of the existing internal layouts, reuse
           ``LineStatementParser(force_format="line" | "hdfc")``; otherwise write a
           dedicated streaming engine exposing ``feed_page`` / ``finalize`` /
           ``opening_balance`` / ``closing_balance`` and drive it here.
        5. Add ``FooBankParser`` to ``_PARSER_CLASSES`` above. Done — no other
           file or parser needs to change.
    """
    key = "example"
    display_name = "Example Bank"
    implemented = False        # keep out of auto-detect until real logic exists

    def detect(self, text_lines: List[str]) -> float:
        # e.g. return 0.9 if any("FOO BANK UNIQUE MARKER" in ln for ln in text_lines) else 0.0
        return 0.0

    def make_engine(self) -> LineStatementParser:
        return LineStatementParser(force_format=None)
