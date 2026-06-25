"""
line_parser.py
==============
PRIMARY transaction parser for single-line statement formats (e.g. Indian Bank),
where each transaction is one dated line:

    DD/MM/YY DD/MM/YY [inline description] <amount> <balance>Cr|Dr

and the surrounding description WRAPS BOTH ABOVE AND BELOW that dated line, e.g.::

    TRANSFER FROM 97157057378            <- description (above)
    /IMPS/P2A/509201599397/ /IMPS/GOO    <- description (above, wrapped mid-word)
    01/04/25 01/04/25 8050.00 119544.23Cr   <- the dated transaction line
    GLEINDIAD /BRANCH : ATM SERVICE      <- description (below, continues the word)
    BRANCH                               <- description (below)

Why not a pure leading/trailing text buffer?
-------------------------------------------
Between two consecutive dated lines you get  [A's trailing ... B's leading]  with
no textual delimiter, so a buffer model leaks B's leading onto A. We instead use
the one unambiguous signal available: vertical position. Every orphan description
line is attached to the **nearest dated line by y-coordinate**; lines above a
dated line become its leading text, lines below become its trailing text, in
top-to-bottom order. This never lets a fragment cross into a neighbour.

Word wraps (the dated line splitting a word, "GOO" + "GLEINDIAD") are rejoined
WITHOUT a space by detecting that the wrapped line reached the description
column's right edge; everything else is joined with a single space. When no
coordinates are available (OCR text) it degrades to single-space joining.

Markers:
    "Brought Forward <amt>Cr"  -> opening balance (seed prev_balance; not a txn)
    "Carried Forward <amt>Cr"  -> page carry / closing marker (not a txn)
    "Closing Balance : <amt>"  -> closing balance (not a txn)
    Summary / "Dr. Count.." / disclaimer / page metadata -> skipped.

Cheque/reference numbers are embedded in the description for this format and are
intentionally left inside Particulars (no separate ref column).
"""

from __future__ import annotations

import re
from typing import List, Optional

from normalizer import clean_amount, parse_date, _MONEY_TOKEN_RE

# --- line / token patterns --------------------------------------------------
_LEAD_DATE = re.compile(r"^\s*(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})")
_WORD_DATE = re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$")
_WORD_MONEY = re.compile(r"^[₹]?[\d,]+\.\d{1,2}(?:cr|dr)?$", re.I)

_OPENING = re.compile(r"brought\s+forward", re.I)
_CLOSING = re.compile(r"carried\s+forward|closing\s+balance", re.I)
_FOOTER = re.compile(
    r"\b(statement|summary)\b|dr\.?\s*count|cr\.?\s*count"
    r"|in\s+case\s+your\s+account|transaction\s+with\s+extra\s+care|page\s*no",
    re.I,
)

_WRAP_TOL = 2.0   # a line within this many points of the column edge "wrapped"

STANDARD_FIELDS = ["date", "value_date", "particulars", "ref_no", "debit", "credit", "balance"]


class PLine:
    """A positioned physical line: text plus optional word boxes for geometry."""
    __slots__ = ("text", "top", "words", "right_x1")

    def __init__(self, text: str, top: float, words: Optional[list] = None) -> None:
        self.text = text
        self.top = top
        self.words = words
        self.right_x1 = max((w.x1 for w in words), default=None) if words else None


def pline_from_words(words: list) -> PLine:
    """Build a PLine from a list of extractor.Word (one physical line)."""
    ordered = sorted(words, key=lambda w: w.x0)
    text = " ".join(w.text for w in ordered)
    top = min(w.top for w in ordered)
    return PLine(text, top, ordered)


def _last_money(text: str) -> Optional[float]:
    matches = list(_MONEY_TOKEN_RE.finditer(text))
    if not matches:
        return None
    val, _ = clean_amount(matches[-1].group())
    return val


def _has_money(text: str) -> bool:
    return _MONEY_TOKEN_RE.search(text) is not None


class LineStatementParser:
    """Streaming, page-by-page parser. Feed pages in order; collect at the end."""

    def __init__(self, opening_balance: Optional[float] = None) -> None:
        self.transactions: List[dict] = []
        self.opening_balance = opening_balance
        self.closing_balance: Optional[float] = None
        self.prev_balance = opening_balance

    # -- public ------------------------------------------------------------
    def feed_page(self, plines: List[PLine]) -> None:
        if not plines:
            return

        # Skip the per-page preamble (bank/account metadata + column header):
        # advance to the first dated line or "Brought Forward".
        start = 0
        while start < len(plines):
            pl = plines[start]
            if self._is_opening(pl.text) or self._parse_dated(pl) is not None:
                break
            start += 1

        dated = []       # [(PLine, parsed dict)] -- the transaction anchor lines
        descs: List[PLine] = []   # orphan description lines on this page
        for pl in plines[start:]:
            text = pl.text
            if self._is_opening(text):
                amt = _last_money(text)
                if amt is not None and self.opening_balance is None:
                    self.opening_balance = amt
                    self.prev_balance = amt
                continue
            if self._is_closing(text):
                amt = _last_money(text)
                if amt is not None:
                    self.closing_balance = amt
                break                          # end of this page's transactions
            if _FOOTER.search(text):
                break                          # summary / disclaimer footer
            parsed = self._parse_dated(pl)
            if parsed is not None:
                dated.append((pl, parsed))
            elif not _has_money(text):
                descs.append(pl)               # genuine description line
            # else: a stray non-dated money line -> ignore

        self._emit(dated, descs)

    def finalize(self) -> List[dict]:
        return self.transactions

    # -- markers -----------------------------------------------------------
    @staticmethod
    def _is_opening(text: str) -> bool:
        return _OPENING.search(text) is not None

    @staticmethod
    def _is_closing(text: str) -> bool:
        return _CLOSING.search(text) is not None

    # -- dated line parsing ------------------------------------------------
    def _parse_dated(self, pl: PLine) -> Optional[dict]:
        text = pl.text
        m1 = _LEAD_DATE.match(text)
        if not m1:
            return None
        date1 = parse_date(m1.group(1))
        if date1 is None:
            return None

        rest = text[m1.end():].lstrip()
        date2 = None
        m2 = _LEAD_DATE.match(rest)
        if m2:
            d2 = parse_date(m2.group(1))
            if d2 is not None:
                date2 = d2
                rest = rest[m2.end():].lstrip()

        money = list(_MONEY_TOKEN_RE.finditer(rest))
        if not money:
            return None                        # a transaction line must show a balance

        balance, b_drcr = clean_amount(money[-1].group())
        if balance is None:
            return None
        if b_drcr == "DR":
            balance = -balance                 # overdrawn balance shown as "...Dr"

        amount = a_drcr = None
        if len(money) >= 2:
            amount, a_drcr = clean_amount(money[-2].group())
            inline = rest[:money[-2].start()].strip()
        else:
            inline = rest[:money[-1].start()].strip()

        # Right edge of the inline description (for wrap detection), from words.
        inline_x1 = None
        if pl.words:
            desc_words = [w for w in pl.words
                          if not _WORD_MONEY.match(w.text) and not _WORD_DATE.match(w.text)]
            if desc_words:
                inline_x1 = max(w.x1 for w in desc_words)

        return {"date": date1, "value_date": date2, "balance": balance,
                "amount": amount, "amount_drcr": a_drcr,
                "inline": inline, "inline_x1": inline_x1}

    # -- assembly ----------------------------------------------------------
    def _emit(self, dated, descs) -> None:
        if not dated:
            return
        dated.sort(key=lambda d: d[0].top)
        tops = [d[0].top for d in dated]

        # Right edge of the description column on this page (for wrap detection).
        edge = max((pl.right_x1 for pl in descs if pl.right_x1 is not None), default=None)

        # Attach each orphan description line to the NEAREST dated line by y.
        buckets: List[List[PLine]] = [[] for _ in dated]
        for dl in descs:
            i = min(range(len(dated)), key=lambda k: abs(tops[k] - dl.top))
            buckets[i].append(dl)

        for (pl, parsed), bucket in zip(dated, buckets):
            bucket.sort(key=lambda x: x.top)
            frags = []   # (text, is_full) in top-to-bottom order
            for d in bucket:
                if d.top < pl.top:                       # above -> leading
                    frags.append((d.text, self._is_full(d.right_x1, edge)))
            if parsed["inline"]:
                frags.append((parsed["inline"], self._is_full(parsed["inline_x1"], edge)))
            for d in bucket:
                if d.top >= pl.top:                       # below -> trailing
                    frags.append((d.text, self._is_full(d.right_x1, edge)))

            particulars = self._assemble(frags)
            self.transactions.append(self._make_txn(parsed, particulars))

    @staticmethod
    def _is_full(x1: Optional[float], edge: Optional[float]) -> bool:
        """The fragment reached the column's right edge -> a word wrapped across."""
        return edge is not None and x1 is not None and x1 >= edge - _WRAP_TOL

    @staticmethod
    def _assemble(frags) -> str:
        if not frags:
            return ""
        out = frags[0][0]
        prev_full = frags[0][1]
        for text, full in frags[1:]:
            out += ("" if prev_full else " ") + text   # wrapped word -> no space
            prev_full = full
        return re.sub(r"\s{2,}", " ", out).strip()

    def _make_txn(self, parsed: dict, particulars: str) -> dict:
        balance = parsed["balance"]
        amount = parsed["amount"]
        drcr = parsed["amount_drcr"]               # explicit suffix on the amount wins

        # Otherwise infer direction from the running-balance movement.
        if drcr is None and amount is not None and self.prev_balance is not None:
            if balance > self.prev_balance:
                drcr = "CR"
            elif balance < self.prev_balance:
                drcr = "DR"

        # No amount token: derive it from the balance delta.
        if amount is None and self.prev_balance is not None:
            delta = balance - self.prev_balance
            if abs(delta) > 0.001:
                amount = abs(delta)
                drcr = "CR" if delta > 0 else "DR"

        debit = credit = None
        if amount is not None:
            if drcr == "DR":
                debit = abs(amount)
            else:                                  # CR or unknown -> credit
                credit = abs(amount)

        self.prev_balance = balance
        return {
            "date": parsed["date"],
            "value_date": parsed["value_date"],
            "particulars": particulars,
            "ref_no": "",                          # refs stay inside Particulars
            "debit": debit,
            "credit": credit,
            "balance": balance,
        }
