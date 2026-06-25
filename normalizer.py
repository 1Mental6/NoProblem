"""
normalizer.py
=============
Turns the positioned rows produced by :mod:`extractor` into a clean, uniform
list of transaction dictionaries with this standard schema::

    date         (datetime)  -- transaction / post date
    value_date   (datetime)  -- second date column when present
    particulars  (str)       -- description, multi-line reassembled in order
    ref_no       (str)       -- cheque / reference / instrument number
    debit        (float)     -- money out  (positive magnitude)
    credit       (float)     -- money in   (positive magnitude)
    balance      (float)     -- running balance (may be negative for overdraft)

It is a *stateful streaming* normalizer (:class:`StatementNormalizer`) fed one
page at a time.

Column assignment (coordinate mode) is the core idea
----------------------------------------------------
When the page came from pdfplumber we have every word with its x-position. We:

    1. find the header line (fuzzy header matching with rapidfuzz),
    2. group the header into cells and record each column's x-centre,
    3. assign every data word to the **nearest column centre** -- i.e. by its
       horizontal position, never by text reading order.

Two date columns side-by-side are both captured: the left one -> ``date``, the
right one -> ``value_date`` (resolved by x-order when both literally say "Date").

Multi-line description reassembly (the critical part)
-----------------------------------------------------
    * A new transaction starts on a line that has a valid date AND at least one
      of (debit, credit, balance).
    * Every following text-only line (no date, no amount) is a continuation and
      its particulars-column text is appended, in original top-to-bottom order,
      to the *current* transaction -- nothing is reordered and fragments cannot
      jump to a neighbouring transaction.

camelot pre-celled rows and OCR text lines are handled by separate, simpler code
paths that share the same cleaning / merge machinery.
"""

from __future__ import annotations

import logging
import re
from statistics import median
from typing import Dict, List, Optional, Tuple

from dateutil import parser as date_parser
from rapidfuzz import fuzz

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Standard schema + header vocabulary
# ---------------------------------------------------------------------------
STANDARD_FIELDS = ["date", "value_date", "particulars", "ref_no", "debit", "credit", "balance"]

HEADER_SYNONYMS: Dict[str, List[str]] = {
    "value_date": ["value date", "val date", "value dt", "value"],
    "date": ["date", "txn date", "transaction date", "tran date", "trans date",
             "posting date", "post date", "tran date", "txn dt", "date of transaction"],
    "particulars": ["particulars", "description", "narration", "transaction details",
                    "details", "remarks", "transaction remarks", "transaction particulars",
                    "narrative", "transaction description", "naration"],
    "ref_no": ["cheque no", "chq no", "ref no", "reference no", "instrument id",
               "cheque/ref no", "chq/ref no", "ref/chq no", "instrument no",
               "cheque number", "reference number", "utr", "ref no/cheque no",
               "chq/ref no", "cheque ref no", "reference", "chq no"],
    "debit": ["debit", "withdrawal", "withdrawal amt", "withdrawal amount", "dr",
              "withdrawals", "debit amount", "paid out", "amount debited",
              "withdrawal dr", "dr amount", "debit dr"],
    "credit": ["credit", "deposit", "deposit amt", "deposit amount", "cr",
               "deposits", "credit amount", "paid in", "amount credited",
               "deposit cr", "cr amount", "credit cr"],
    "balance": ["balance", "closing balance", "running balance", "available balance",
                "balance amount", "bal", "balance inr", "closing bal"],
    # The two below only appear when a bank uses ONE amount column:
    "amount": ["amount", "amt", "transaction amount", "txn amount", "amount inr"],
    "dr_cr": ["dr/cr", "type", "drcr", "cr/dr", "indicator", "transaction type",
              "dr / cr", "debit/credit", "d/c"],
}

_HEADER_THRESHOLD = 80        # rapidfuzz score required to accept a header match
_MIN_HEADER_FIELDS = 2
_FALLBACK_MIN_TXN_LINES = 2   # transaction-like lines needed to trigger text fallback

_NOISE_PATTERNS = [
    re.compile(r"page\s*\d+\s*(of|/)\s*\d+", re.I),
    re.compile(r"this is a (computer|system)[ -]generated", re.I),
    re.compile(r"statement of account", re.I),
    re.compile(r"^\s*page\s*\d+\s*$", re.I),
    re.compile(r"please examine|kindly verify|in case of any discrepancy", re.I),
    re.compile(r"registered office|regd\.? office|www\.|http", re.I),
    re.compile(r"end of statement|continued on next page", re.I),
]
_OPENING_PATTERNS = [re.compile(r"opening balance|brought forward|^\s*b[/. ]?f\b", re.I)]
_SUMMARY_PATTERNS = [
    re.compile(r"closing balance|carried forward|^\s*c[/. ]?f\b", re.I),
    re.compile(r"\btotal\b|grand total|statement summary|sub[- ]total", re.I),
]

_CURRENCY_RE = re.compile(r"(₹|rs\.?|inr|\$)", re.I)
_DRCR_RE = re.compile(r"(?<![a-zA-Z])(cr|dr)(?![a-zA-Z])\.?", re.I)
_MONEY_TOKEN_RE = re.compile(r"[+\-(]?\s*(?:₹|rs\.?|inr)?\s*[\d,]+\.\d{1,2}\s*(?:cr|dr)?\)?", re.I)

_DATE_REGEXES = [
    re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$"),
    re.compile(r"^\d{1,2}[-/ .][A-Za-z]{3,9}[-/ .]\d{2,4}$"),
    re.compile(r"^[A-Za-z]{3,9}[-/ .]\d{1,2}[,]?[-/ .]?\d{2,4}$"),
    re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$"),
    re.compile(r"^\d{1,2}[A-Za-z]{3}\d{2,4}$"),
]
_LEADING_DATE_RE = re.compile(
    r"^\s*("
    r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
    r"|\d{1,2}[-/ ][A-Za-z]{3,9}[-/ ]\d{2,4}"
    r"|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r")"
)


# ===========================================================================
# Value cleaning helpers
# ===========================================================================
def clean_amount(value) -> Tuple[Optional[float], Optional[str]]:
    """Parse a messy money string -> ``(signed_float | None, 'DR'|'CR'|None)``.

    Handles ``₹``/``Rs.``, commas (incl. Indian 1,00,000 grouping), trailing
    ``Cr``/``Dr`` (even glued to digits), leading/trailing ``-`` and ``(...)``.
    """
    if value is None:
        return None, None
    s = str(value).strip()
    if not s or s.lower() in {"-", "--", "na", "n/a", "nil", "none", "."}:
        return None, None

    dr_cr: Optional[str] = None
    neg = False

    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()

    s = _CURRENCY_RE.sub("", s).strip()

    m = _DRCR_RE.search(s)
    if m:
        dr_cr = "CR" if m.group(1).upper() == "CR" else "DR"
        s = _DRCR_RE.sub("", s).strip()

    s = s.replace(" ", "")
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("-"):
        neg = True
        s = s[1:]
    if s.endswith("-"):
        neg = True
        s = s[:-1]

    s = s.replace(",", "")
    if not re.fullmatch(r"\d*\.?\d+", s):
        return None, dr_cr

    try:
        val = float(s)
    except ValueError:
        return None, dr_cr

    if neg:
        val = -val
        if dr_cr is None:
            dr_cr = "DR"
    return val, dr_cr


def parse_date(value) -> Optional[object]:
    """Auto-detect format and return a ``datetime`` (or ``None``).

    A regex pre-filter stops dateutil from turning stray numbers into dates, and
    ``dayfirst=True`` matches day-first Indian statements (DD/MM/YY etc.).
    """
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value).strip())
    if not s:
        return None
    compact = s.replace(" ", "")
    if not (any(rx.match(s) for rx in _DATE_REGEXES) or any(rx.match(compact) for rx in _DATE_REGEXES)):
        return None
    for candidate in (s, compact):
        try:
            return date_parser.parse(candidate, dayfirst=True, fuzzy=False)
        except (ValueError, OverflowError, TypeError):
            continue
    return None


# ===========================================================================
# Header matching
# ===========================================================================
def _best_field(cell: str) -> Tuple[Optional[str], float]:
    """Best canonical field for a header cell via fuzzy phrase matching.

    Uses ``token_sort_ratio`` (order-insensitive) rather than ``token_set_ratio``
    on purpose: the latter scores the subset "date" against "value date" as 100,
    which would steal the plain "Date" column. Ties break toward the phrase
    closest in length, so exact "date" beats the longer "value date".
    """
    cell_norm = re.sub(r"\s+", " ", (cell or "").strip().lower()).replace(".", "").strip()
    if not cell_norm:
        return None, 0.0
    best_field, best_score, best_lendiff = None, 0.0, 1e9
    for fld, phrases in HEADER_SYNONYMS.items():
        for phrase in phrases:
            score = max(fuzz.WRatio(cell_norm, phrase), fuzz.token_sort_ratio(cell_norm, phrase))
            lendiff = abs(len(cell_norm) - len(phrase))
            if score > best_score or (score == best_score and lendiff < best_lendiff):
                best_field, best_score, best_lendiff = fld, score, lendiff
    return best_field, best_score


def map_headers(header_row: List[str]) -> Dict[str, int]:
    """Map an index-based header row (camelot) to ``{field: column_index}``."""
    mapping: Dict[str, int] = {}
    scores: Dict[str, float] = {}
    date_cols: List[Tuple[int, float]] = []
    for idx, cell in enumerate(header_row):
        fld, score = _best_field(cell)
        if fld is None or score < _HEADER_THRESHOLD:
            continue
        if fld == "date":
            date_cols.append((idx, score))
            continue
        if fld not in mapping or score > scores.get(fld, 0):
            mapping[fld] = idx
            scores[fld] = score
    # Resolve possibly-two date columns by left-to-right order.
    date_cols.sort(key=lambda t: t[0])
    if date_cols:
        mapping["date"] = date_cols[0][0]
        if len(date_cols) > 1 and "value_date" not in mapping:
            mapping["value_date"] = date_cols[1][0]
    return mapping


# ===========================================================================
# Streaming normalizer
# ===========================================================================
class StatementNormalizer:
    def __init__(self) -> None:
        # coordinate mode
        self.columns: Optional[List[dict]] = None
        self._coord_header_sigs: set = set()      # signatures of each header line
        self._coord_text_fallback = False         # header undetectable -> text mode
        # index mode (camelot)
        self.index_map: Optional[Dict[str, int]] = None
        # shared
        self.header_signature: Optional[str] = None
        self.transactions: List[dict] = []
        self.current: Optional[dict] = None
        self.opening_balance: Optional[float] = None
        self.ocr_used = False
        self._last_balance: Optional[float] = None

    # -- public ------------------------------------------------------------
    def feed_page(self, page_data) -> None:
        if getattr(page_data, "ocr_used", False):
            self.ocr_used = True
        if page_data.lines:
            self._feed_lines(page_data.lines)
        elif page_data.rows:
            self._feed_rows(page_data.rows)
        elif page_data.text_lines:
            self._feed_text_lines(page_data.text_lines)

    def finalize(self) -> List[dict]:
        self._flush_current()
        return self.transactions

    # ===================================================================
    # COORDINATE MODE (pdfplumber)
    # ===================================================================
    def _feed_lines(self, lines: List[List]) -> None:
        # A previous page failed header detection -> stay in text-line mode.
        if self._coord_text_fallback:
            self._feed_text_lines([_join_words(ln) for ln in lines])
            return

        i, n = 0, len(lines)
        while i < n:
            words = lines[i]
            if not words:
                i += 1
                continue

            if self.columns is None:
                consumed = self._try_establish_header(lines, i)
                if consumed:
                    i += consumed       # 1 (single) or 2 (stacked) header lines
                    continue
                i += 1                  # not a header -> keep looking
                continue

            if self._is_repeated_header_words(words):
                i += 1
                continue

            row_text = _join_words(words).strip()
            cells = self._assign_cells(words)
            txn = self._cells_to_txn(cells)
            self._route(txn, cells, row_text, words)
            i += 1

        # Safety net: never silently drop a page full of transactions just
        # because its header layout wasn't recognized. Re-run this page (and all
        # later pages) through the text-line parser instead of discarding it.
        if (self.columns is None and not self._coord_text_fallback
                and _page_has_transactions(lines)):
            _LOG.warning(
                "Header detection failed on a transactional page; positional "
                "column mapping skipped -- falling back to text-line parsing."
            )
            self._coord_text_fallback = True
            self._feed_text_lines([_join_words(ln) for ln in lines])

    def _try_establish_header(self, lines: List[List], i: int) -> int:
        """Try to lock in the column header starting at physical line *i*.

        Returns the number of physical lines consumed: 1 for a normal single-line
        header, 2 when a header split across two stacked lines is merged, or 0 if
        line *i* is not (part of) a header.

        Stacked headers (e.g. "Post Date" on one line with a second "Date" on the
        line below, while Debit/Credit/Balance sit on the first line) are handled
        by combining the two lines' word lists. Each word keeps its own
        x-coordinate, so the stacked "Date" lands in the value-date column by its
        horizontal position.

        A line is only merged with the next one when that next line is a genuine
        header *continuation* -- it carries header tokens, is vertically adjacent
        (stacked, not a far-away title/subtitle), and is not itself a transaction
        row. This both fixes stacked headers AND avoids fusing a nearby
        title/subtitle or the first data row into the header.
        """
        words = lines[i]
        strict_alone = self._looks_like_header_words(words)

        # Look for an adjacent header-continuation line to merge in. This runs
        # even when the first line already passes strict, so a second "Date"
        # stacked under "Post Date" still enriches the columns with value_date.
        merged_words = None
        if i + 1 < len(lines):
            nxt = lines[i + 1]
            if (self._header_fields(nxt)                    # carries header words
                    and not _line_is_transactional(nxt)     # ...not a data row
                    and _lines_adjacent(words, nxt)):        # ...stacked tight
                candidate = list(words) + list(nxt)
                if (self._looks_like_header_words(candidate)
                        or self._looks_like_header_words(candidate, lenient=True)):
                    merged_words = candidate

        enough_tokens = strict_alone or len(self._header_fields(words)) >= _MIN_HEADER_FIELDS
        if merged_words is not None and enough_tokens:
            self._lock_header(merged_words, [words, lines[i + 1]])
            return 2
        if strict_alone:
            self._lock_header(words, [words])
            return 1
        return 0

    def _lock_header(self, header_words: List, physical_lines: List[List]) -> None:
        """Build columns and remember each header line's signature for stripping
        a repeated (possibly two-line) header on later pages."""
        self._build_columns(header_words)
        self._coord_header_sigs = {_sig_words(pl) for pl in physical_lines}
        self._coord_header_sigs.add(_sig_words(header_words))

    def _build_columns(self, header_words: List) -> None:
        cells = _group_cells(header_words)
        columns: List[dict] = []
        for text, x0, x1, center in cells:
            fld, score = _best_field(text)
            columns.append({
                "field": fld if (fld and score >= _HEADER_THRESHOLD) else None,
                "text": text, "x0": x0, "x1": x1, "center": center,
            })
        columns.sort(key=lambda c: c["center"])
        self._resolve_date_columns(columns)
        self.columns = columns

    @staticmethod
    def _resolve_date_columns(columns: List[dict]) -> None:
        """Two side-by-side date columns: left -> date, right -> value_date."""
        date_cols = [c for c in columns if c["field"] == "date"]
        has_value = any(c["field"] == "value_date" for c in columns)
        if len(date_cols) >= 2 and not has_value:
            date_cols.sort(key=lambda c: c["center"])
            date_cols[1]["field"] = "value_date"
            for extra in date_cols[2:]:
                extra["field"] = None

    def _assign_cells(self, words: List) -> Dict[str, str]:
        """Group the line into cells, then assign each cell to its nearest column.

        Grouping first (rather than placing individual words) is what makes wide
        multi-word columns robust: a whole description groups into ONE cell whose
        centre sits squarely under the Particulars header, instead of its right-
        hand words drifting into the next column. Large inter-column gaps keep
        cheque numbers and amounts in their own cells.
        """
        assert self.columns is not None
        centers = [c["center"] for c in self.columns]
        cells: Dict[str, str] = {}
        for text, x0, x1, center in _group_cells(words):
            idx = min(range(len(centers)), key=lambda i: abs(centers[i] - center))
            field = self.columns[idx]["field"]
            if not field:
                continue  # cell fell under an unmapped column -> discard
            cells[field] = (cells[field] + " " + text).strip() if field in cells else text
        return cells

    def _cells_to_txn(self, cells: Dict[str, str]) -> dict:
        txn = {f: None for f in STANDARD_FIELDS}
        txn["particulars"] = cells.get("particulars", "") or ""
        txn["ref_no"] = cells.get("ref_no", "") or ""
        txn["date"] = parse_date(cells.get("date"))
        txn["value_date"] = parse_date(cells.get("value_date"))

        bal, _ = clean_amount(cells.get("balance"))
        txn["balance"] = bal

        if "debit" in cells or "credit" in cells:
            d, _ = clean_amount(cells.get("debit"))
            c, _ = clean_amount(cells.get("credit"))
            txn["debit"] = abs(d) if d is not None else None
            txn["credit"] = abs(c) if c is not None else None
        elif "amount" in cells:
            amt, hint = clean_amount(cells.get("amount"))
            drcr = self._read_indicator(cells.get("dr_cr")) or hint
            self._apply_amount(txn, amt, drcr)
        return txn

    def _is_repeated_header_words(self, words: List) -> bool:
        return _sig_words(words) in self._coord_header_sigs

    @staticmethod
    def _header_fields(words: List) -> set:
        """Distinct standard fields the cells of *words* map to (above threshold)."""
        fields = set()
        for text, *_ in _group_cells(words):
            fld, score = _best_field(text)
            if fld and score >= _HEADER_THRESHOLD:
                fields.add(fld)
        return fields

    @classmethod
    def _looks_like_header_words(cls, words: List, lenient: bool = False) -> bool:
        """Does this (possibly merged) line look like the column header?

        Strict (default): >=2 mapped fields AND an anchor (date/particulars) AND a
        money column -- all on the one (merged) line.

        Lenient safety net (only used after a stacked-header merge was attempted):
        >=2 mapped fields where at least one is a money column or a date, even if a
        clear anchor+money pair isn't present together.
        """
        fields = cls._header_fields(words)
        if len(fields) < _MIN_HEADER_FIELDS:
            return False
        money = bool(fields & {"debit", "credit", "balance", "amount"})
        if lenient:
            return money or ("date" in fields)
        anchor = "date" in fields or "particulars" in fields
        return anchor and money

    # ===================================================================
    # INDEX MODE (camelot pre-celled rows)
    # ===================================================================
    def _feed_rows(self, rows: List[List[str]]) -> None:
        for row in rows:
            if not any((c or "").strip() for c in row):
                continue
            if self.index_map is None:
                if _looks_like_header_row(row):
                    self.index_map = map_headers(row)
                    self.header_signature = _sig_row(row)
                continue
            if self.header_signature and _sig_row(row) == self.header_signature:
                continue
            cells = self._row_to_cells(row)
            txn = self._cells_to_txn(cells)
            row_text = " ".join((c or "").strip() for c in row).strip()
            self._route(txn, cells, row_text, None)

    def _row_to_cells(self, row: List[str]) -> Dict[str, str]:
        m = self.index_map or {}
        cells: Dict[str, str] = {}
        for field, idx in m.items():
            if idx < len(row) and row[idx] and row[idx].strip():
                cells[field] = row[idx].strip()
        return cells

    # ===================================================================
    # TEXT / OCR MODE
    # ===================================================================
    def _feed_text_lines(self, lines: List[str]) -> None:
        for line in lines:
            s = line.strip()
            if not s or self._is_noise(s):
                continue
            # Opening "Brought Forward" line (balance, no date) -> capture it.
            if (self.current is None and self.opening_balance is None
                    and self._classify(s) == "opening"):
                bal = self._last_money(s)
                if bal is not None:
                    self.opening_balance = bal
                    self._last_balance = bal
                    continue
            txn = self._parse_text_line(s)
            if txn and self._is_new_transaction(txn):
                self._commit(txn)
            elif self.current is not None:
                self._append_particulars(self.current, s)

    @staticmethod
    def _last_money(s: str) -> Optional[float]:
        matches = list(_MONEY_TOKEN_RE.finditer(s))
        if not matches:
            return None
        val, _ = clean_amount(matches[-1].group())
        return val

    def _parse_text_line(self, line: str) -> Optional[dict]:
        m = _LEADING_DATE_RE.match(line)
        if not m:
            return None
        date = parse_date(m.group(1))
        if date is None:
            return None
        rest = line[m.end():].strip()
        money = list(_MONEY_TOKEN_RE.finditer(rest))

        txn = {f: None for f in STANDARD_FIELDS}
        txn["date"] = date
        txn["ref_no"] = ""
        if money:
            balance, _ = clean_amount(money[-1].group())
            txn["balance"] = balance
            if len(money) >= 2:
                amount, hint = clean_amount(money[-2].group())
                drcr = hint
                if drcr is None and balance is not None and self._last_balance is not None:
                    drcr = "DR" if balance < self._last_balance else "CR"
                self._apply_amount(txn, amount, drcr)
            cut = money[-2].start() if len(money) >= 2 else money[-1].start()
            txn["particulars"] = rest[:cut].strip()
        else:
            txn["particulars"] = rest
        return txn

    # ===================================================================
    # Shared routing / merge / lifecycle
    # ===================================================================
    def _route(self, txn: dict, cells: Dict[str, str], row_text: str, words) -> None:
        """Decide whether a parsed row is opening / new txn / continuation / noise."""
        kind = self._classify(row_text)

        # 1. Opening balance: balance present, no debit/credit (may or may not have a date).
        if kind == "opening" and txn["balance"] is not None \
                and txn["debit"] is None and txn["credit"] is None:
            self._flush_current()
            self.opening_balance = txn["balance"]
            self._last_balance = txn["balance"]
            return

        # 2. New transaction: valid date AND at least one amount.
        if self._is_new_transaction(txn):
            self._commit(txn)
            return

        # 3. Summary / footer / page noise -> drop.
        if kind in ("summary", "noise", "opening"):
            return

        # 4. A no-date row that still carries an amount is NOT a continuation
        #    (per spec); drop it rather than risk corrupting a description.
        if any(txn[f] is not None for f in ("debit", "credit", "balance")):
            return

        # 5. Genuine continuation: text-only -> append to the current transaction.
        if self.current is None:
            return
        text = (cells.get("particulars") or "").strip()
        if not text and words:
            text = " ".join(w.text for w in words).strip()
        if not text and not words and row_text:
            text = row_text
        self._append_particulars(self.current, text)

    @staticmethod
    def _is_new_transaction(txn: dict) -> bool:
        return txn["date"] is not None and any(
            txn[f] is not None for f in ("debit", "credit", "balance")
        )

    @staticmethod
    def _append_particulars(cur: dict, text: str) -> None:
        if text:
            cur["particulars"] = (cur.get("particulars", "") + " " + text).strip()

    @staticmethod
    def _read_indicator(tag: Optional[str]) -> Optional[str]:
        if not tag:
            return None
        if re.search(r"cr", tag, re.I):
            return "CR"
        if re.search(r"dr", tag, re.I):
            return "DR"
        return None

    @staticmethod
    def _apply_amount(txn: dict, amt: Optional[float], drcr: Optional[str]) -> None:
        if amt is None:
            return
        if drcr == "CR":
            txn["credit"] = abs(amt)
        elif drcr == "DR":
            txn["debit"] = abs(amt)
        elif amt < 0:
            txn["debit"] = abs(amt)
        else:
            txn["credit"] = abs(amt)

    @staticmethod
    def _classify(row_text: str) -> str:
        if not row_text.strip():
            return "noise"
        for rx in _OPENING_PATTERNS:
            if rx.search(row_text):
                return "opening"
        for rx in _SUMMARY_PATTERNS:
            if rx.search(row_text):
                return "summary"
        for rx in _NOISE_PATTERNS:
            if rx.search(row_text):
                return "noise"
        return "continuation"

    def _is_noise(self, text: str) -> bool:
        return self._classify(text) in ("noise", "summary")

    def _commit(self, txn: dict) -> None:
        self._flush_current()
        txn["particulars"] = re.sub(r"\s+", " ", (txn.get("particulars") or "")).strip()
        self.current = txn
        if txn.get("balance") is not None:
            self._last_balance = txn["balance"]

    def _flush_current(self) -> None:
        if self.current is not None:
            self.current["particulars"] = re.sub(
                r"\s+", " ", (self.current.get("particulars") or "")
            ).strip()
            self.transactions.append(self.current)
            self.current = None


# ===========================================================================
# Module-level helpers
# ===========================================================================
def _join_words(words: List) -> str:
    """Flatten a line's word boxes back into plain text (left-to-right)."""
    return " ".join(w.text for w in sorted(words, key=lambda w: w.x0))


def _line_is_transactional(line: List) -> bool:
    """True if a line looks like a data row (leading date value + a money token)."""
    text = _join_words(line)
    return bool(_LEADING_DATE_RE.match(text) and _MONEY_TOKEN_RE.search(text))


def _lines_adjacent(line_a: List, line_b: List) -> bool:
    """True if *line_b* sits directly under *line_a* (stacked, ~one line apart).

    Distinguishes a stacked two-line header from a title/subtitle separated by a
    larger vertical gap, using the words' own heights as the scale.
    """
    if not line_a or not line_b:
        return False
    heights = [w.bottom - w.top for w in line_a] + [w.bottom - w.top for w in line_b]
    h = median(heights) if heights else 8.0
    gap = min(w.top for w in line_b) - max(w.bottom for w in line_a)
    return gap <= h * 1.5


def _page_has_transactions(lines: List[List]) -> bool:
    """True if the page clearly holds transaction-like rows (leading date + money).

    Used to decide whether to fall back to the text-line parser when no column
    header could be detected, so a transactional page is never silently dropped.
    """
    count = 0
    for ln in lines:
        if _line_is_transactional(ln):
            count += 1
            if count >= _FALLBACK_MIN_TXN_LINES:
                return True
    return False


def _group_cells(words: List) -> List[Tuple[str, float, float, float]]:
    """Group a line's words into header cells by horizontal gaps.

    The gap threshold adapts to the font (multiples of the average character
    width) so multi-word headers ("Value Date", "Withdrawal Amt") stay together
    while genuine column gaps split.
    """
    ws = sorted(words, key=lambda w: w.x0)
    if not ws:
        return []
    char_widths = [(w.x1 - w.x0) / max(len(w.text), 1) for w in ws]
    avg_char = median(char_widths) if char_widths else 5.0
    threshold = max(avg_char * 2.5, 6.0)

    groups: List[List] = [[ws[0]]]
    for prev, w in zip(ws, ws[1:]):
        if w.x0 - prev.x1 > threshold:
            groups.append([w])
        else:
            groups[-1].append(w)

    return [(" ".join(x.text for x in g), g[0].x0, g[-1].x1, (g[0].x0 + g[-1].x1) / 2.0)
            for g in groups]


def _looks_like_header_row(row: List[str]) -> bool:
    fields = set()
    for cell in row:
        fld, score = _best_field(cell)
        if fld and score >= _HEADER_THRESHOLD:
            fields.add(fld)
    anchor = "date" in fields or "particulars" in fields
    money = bool(fields & {"debit", "credit", "balance", "amount"})
    return len(fields) >= _MIN_HEADER_FIELDS and anchor and money


def _sig_words(words: List) -> str:
    return "|".join(re.sub(r"\s+", " ", w.text.strip().lower()) for w in words)


def _sig_row(row: List[str]) -> str:
    return "|".join(re.sub(r"\s+", " ", (c or "").strip().lower()) for c in row)


def normalize_pages(pages) -> Tuple[List[dict], dict]:
    """Convenience: normalize an iterable of PageData. Returns ``(txns, meta)``."""
    norm = StatementNormalizer()
    for page in pages:
        norm.feed_page(page)
    txns = norm.finalize()
    return txns, {"opening_balance": norm.opening_balance, "ocr_used": norm.ocr_used}
