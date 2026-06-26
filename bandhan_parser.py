"""
bandhan_parser.py
=================
Dedicated streaming engine for **Bandhan Bank** statements.

Bandhan's defining quirk is that each transaction's two dates are SPLIT across
two physical lines: the day-month halves sit on the transaction's first line and
the bare years on the line directly below::

    02-APR- 02-APR- 000005 CHQ PAID-TP-CW 90,000.00 0.00 12,607.00   <- line A
    2025    2025    GUDDU MAJHI -                                    <- line B (years + desc)
    SILIGURI 000005                                                 <- line C (desc cont.)

So line A starts with two partial dates ``DD-MMM- DD-MMM-`` and line B starts
with two bare years ``YYYY YYYY``; we stitch them (``02-APR-`` + ``2025`` ->
``02-APR-2025``). After the partial dates, line A carries an OPTIONAL
cheque/instrument number (a pure-digit token), then description text, then the
THREE trailing money tokens which are ALWAYS ``Debit Credit Balance`` in that
order (an empty column shows ``0.00``, never a dash; balance may be negative).
Debit vs credit is read positionally (a ``0.00`` column means "not this side"),
never by guessing from the balance direction.

The remainder of line B and any further plain lines (line C, ...) are description
continuation, appended in order until the next transaction (a new
``DD-MMM- DD-MMM-`` line). ``B/F`` (Brought Forward) is captured as the opening
balance and not emitted; the repeated column header and the DICGC /
"computer generated statement" disclaimer block are skipped as noise.

Because a description can wrap across a page boundary (past the per-page footer
and the repeated header), the engine buffers the physical lines fed to it and
parses the whole ordered stream in :meth:`finalize`. Bandhan personal-account
statements are small, so this costs nothing while keeping cross-page rows intact.
"""

from __future__ import annotations

import re
from typing import List, Optional

from normalizer import clean_amount, parse_date

# A partial date "DD-MMM-" whose year has wrapped to the next physical line.
_PARTIAL_DATE = r"\d{1,2}-[A-Za-z]{3}-"
_TXN_START = re.compile(rf"^\s*({_PARTIAL_DATE})\s+({_PARTIAL_DATE})\s*(.*)$")
_YEAR_LINE = re.compile(r"^\s*(\d{4})\s+(\d{4})\b\s*(.*)$")
_MONEY = re.compile(r"^-?[\d,]+\.\d{1,2}$")

# The two-line column header ("TRANS VALUE ... DEBITS CREDITS BALANCE" /
# "DATE DATE INSTRUMENT"), repeated atop every page.
_HEADER = re.compile(r"\bINSTRUMENT\b|TRANS.*VALUE.*DEBITS.*CREDITS", re.I)
_HEADER_DETECT = re.compile(r"TRANS.*VALUE.*DEBITS.*CREDITS", re.I)
# The DICGC deposit-insurance disclaimer paragraph + sign-off (footer noise).
_FOOTER = re.compile(
    r"depositor|DICGC|deposit insurance|most important document|schedule of charges"
    r"|constituent notifies|computer generated|requires no signature",
    re.I,
)
_BF = re.compile(r"\bB\s*/\s*F\b|brought\s+forward", re.I)


def detect_confidence(text_lines: List[str]) -> float:
    """Confidence that *text_lines* are a Bandhan statement (for the registry)."""
    has_header = any(_HEADER_DETECT.search(ln or "") for ln in text_lines)
    has_split = any(
        _TXN_START.match(text_lines[k] or "") and _YEAR_LINE.match((text_lines[k + 1] or "").strip())
        for k in range(len(text_lines) - 1)
    )
    if has_split and has_header:
        return 0.98
    if has_split:                       # the split-date pattern alone is already unique
        return 0.90
    return 0.0


class BandhanStatementParser:
    """Streaming engine: buffer pages, then parse the ordered line stream."""

    def __init__(self, opening_balance: Optional[float] = None) -> None:
        self.transactions: List[dict] = []
        self.opening_balance = opening_balance
        self.closing_balance: Optional[float] = None
        self.prev_balance = opening_balance
        self._lines: list = []

    def feed_page(self, plines: list) -> None:
        self._lines.extend(pl for pl in plines if pl.text and pl.text.strip())

    def finalize(self) -> List[dict]:
        lines = self._lines
        n = len(lines)
        i = 0
        current: Optional[dict] = None
        skipping_footer = False
        while i < n:
            text = lines[i].text.strip()
            start = _TXN_START.match(text)
            if start:
                self._flush(current)
                current = None
                skipping_footer = False
                current, i = self._consume_transaction(lines, i, start)
                continue
            if skipping_footer:
                i += 1
                continue
            if _FOOTER.search(text):
                skipping_footer = True            # skip the disclaimer block to the next txn
                i += 1
                continue
            if _HEADER.search(text):
                i += 1
                continue
            if current is not None:               # plain line -> description continuation
                current["desc"].append(text)
            i += 1
        self._flush(current)
        return self.transactions

    def _consume_transaction(self, lines: list, i: int, start) -> tuple:
        """Parse the transaction anchored at line *i*; return (current|None, next_i)."""
        partial1, partial2, rest_a = start.group(1), start.group(2), start.group(3)

        # The very next line carries the two wrapped years (+ description cont.).
        j = i + 1
        year1 = year2 = ""
        rest_b = ""
        if j < len(lines):
            ym = _YEAR_LINE.match(lines[j].text.strip())
            if ym:
                year1, year2, rest_b = ym.group(1), ym.group(2), ym.group(3)
                j += 1

        trans_date = parse_date(partial1 + year1)
        value_date = parse_date(partial2 + year2)
        cheque, desc_a, debit, credit, balance = self._parse_anchor_tail(rest_a)
        if balance is None:                       # not a real transaction line -> skip
            return None, j

        if _BF.search(rest_a):                     # Brought Forward -> opening balance only
            if self.opening_balance is None:
                self.opening_balance = balance
            self.prev_balance = balance
            return None, j

        current = {
            "date": trans_date, "value_date": value_date, "cheque": cheque,
            "debit": debit, "credit": credit, "balance": balance,
            "desc": [d for d in (desc_a, rest_b) if d],
        }
        return current, j

    @staticmethod
    def _parse_anchor_tail(rest_a: str) -> tuple:
        """Split line A's tail into (cheque, description, debit, credit, balance).

        The last three money tokens are Debit, Credit, Balance positionally; an
        optional leading pure-digit token is the cheque/instrument number; a
        ``0.00`` debit or credit column means that side is empty.
        """
        tokens = rest_a.split()
        money_idx = [k for k, t in enumerate(tokens) if _MONEY.match(t)]
        if len(money_idx) < 3:
            return "", rest_a.strip(), None, None, None

        d_i, c_i, b_i = money_idx[-3], money_idx[-2], money_idx[-1]
        debit = clean_amount(tokens[d_i])[0]
        credit = clean_amount(tokens[c_i])[0]
        balance = clean_amount(tokens[b_i])[0]

        head = tokens[:d_i]
        cheque = ""
        if head and head[0].isdigit():
            cheque, head = head[0], head[1:]
        desc_a = " ".join(head)

        debit = None if (debit is not None and abs(debit) < 1e-9) else debit
        credit = None if (credit is not None and abs(credit) < 1e-9) else credit
        return cheque, desc_a, debit, credit, balance

    def _flush(self, current: Optional[dict]) -> None:
        if current is None:
            return
        particulars = re.sub(r"\s{2,}", " ", " ".join(current["desc"])).strip()
        self.prev_balance = current["balance"]
        self.closing_balance = current["balance"]
        self.transactions.append({
            "date": current["date"],
            "value_date": current["value_date"],
            "particulars": particulars,
            "ref_no": current["cheque"],
            "debit": current["debit"],
            "credit": current["credit"],
            "balance": current["balance"],
        })
