"""
canara_parser.py
================
Dedicated streaming engine for **Canara Bank** statements.

Layout (header: TRANS DATE | VALUE DATE | BRANCH | REF/CHQ.NO | DESCRIPTION |
WITHDRAWS | DEPOSIT | BALANCE)::

    01-APR-25 01-APR-25 0    B/F ...                 3,023,185.24 0.00      -3,023,185.24
    29-APR-25 29-APR-25 33   539471459250 UPI/CR/... 0.00         34,000.00 -2,999,185.24
                             SA/IBKL/**AUTAM@YBL/PAYMENT  ...   (description wraps below)

Unlike Bandhan, the two dates are COMPLETE (``DD-MMM-YY``) and sit on the
transaction's own line. After the two dates come:

    * BRANCH   -- a number, always present (e.g. ``0``, ``33``, ``6396``);
    * REF/CHQ  -- an OPTIONAL long number (UPI/transfer reference);
    * DESCRIPTION text (may wrap over several following lines); then
    * the THREE trailing money tokens, always Withdraws, Deposit, Balance.

Withdraws -> Debit, Deposit -> Credit, read POSITIONALLY (an empty column shows
``0.00``, never a dash; the balance may be NEGATIVE -- this is an overdraft
account). The long Ref number goes into Cheque/Ref No; the Branch code is kept as
``Br:<n>`` in Cheque/Ref No when there is no Ref -- either way Branch never leaks
into Particulars or the money columns. ``B/F`` is captured as the opening balance
and not emitted. Account metadata above the header has no leading date and is
auto-filtered; the repeated header, per-page ``Confidential`` + page number, and
the trailing Statement Summary / disclaimer block are skipped.

Descriptions can wrap across a page boundary, so the engine buffers the physical
lines and parses the ordered stream in :meth:`finalize`.
"""

from __future__ import annotations

import re
from typing import List, Optional

from normalizer import clean_amount, parse_date

# A complete date "DD-MMM-YY" (year present, unlike Bandhan's split halves).
_DATE = r"\d{1,2}-[A-Za-z]{3}-\d{2,4}"
_TXN_START = re.compile(rf"^\s*({_DATE})\s+({_DATE})\s+(.*)$")
_MONEY = re.compile(r"^-?[\d,]+\.\d{1,2}$")

# Repeated column header ("TRANS VALUE BRANCH ... WITHDRAWS DEPOSIT BALANCE" /
# "DATE DATE"); "WITHDRAWS" is Canara-specific.
_HEADER = re.compile(r"WITHDRAWS|TRANS\s+VALUE|^DATE\s+DATE\b", re.I)
_HEADER_DETECT = re.compile(r"WITHDRAWS.*DEPOSIT.*BALANCE", re.I)
# Per-page page-number line (skip transparently so cross-page rows stay intact).
_PAGE_NUM = re.compile(r"^\d{1,4}$")
_PAGE_FOOTER = re.compile(r"confidential", re.I)
# Trailing summary / disclaimer block -> skip everything after it.
_END = re.compile(
    r"statement\s+summary|end\s+of\s+statement|computer\s+output|constituent"
    r"|phishing|ombudsman|do\s+not\s+share|beware|clear\s+balance\s+may",
    re.I,
)
_BF = re.compile(r"\bB\s*/\s*F\b|brought\s+forward", re.I)


def detect_confidence(text_lines: List[str]) -> float:
    """Confidence that *text_lines* are a Canara statement (for the registry)."""
    has_header = any(_HEADER_DETECT.search(ln or "") for ln in text_lines)
    has_txn = any(_TXN_START.match(ln or "") for ln in text_lines)
    if has_header and has_txn:
        return 0.98
    if has_header:
        return 0.60
    return 0.0


class CanaraStatementParser:
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
        ended = False                              # past the Statement Summary block
        while i < n:
            text = lines[i].text.strip()
            start = _TXN_START.match(text)
            if start and not ended:
                self._flush(current)
                current = self._make_transaction(start)
                i += 1
                continue
            if ended:
                i += 1
                continue
            if _END.search(text):                  # summary / disclaimer -> stop emitting
                self._flush(current)
                current = None
                ended = True
                i += 1
                continue
            if _HEADER.search(text) or _PAGE_FOOTER.search(text) or _PAGE_NUM.match(text):
                i += 1                              # repeated header / page footer / page no.
                continue
            if current is not None:                 # plain line -> description continuation
                current["desc"].append(text)
            i += 1
        self._flush(current)
        return self.transactions

    def _make_transaction(self, start) -> Optional[dict]:
        """Build a transaction dict from a matched start line (or None for B/F)."""
        trans_date = parse_date(start.group(1))
        value_date = parse_date(start.group(2))
        branch, ref, desc, debit, credit, balance = self._parse_anchor_tail(start.group(3))
        if balance is None:
            return None
        if _BF.search(start.group(3)):             # Brought Forward -> opening balance only
            if self.opening_balance is None:
                self.opening_balance = balance
            self.prev_balance = balance
            return None
        return {
            "date": trans_date, "value_date": value_date,
            "ref_no": ref or (f"Br:{branch}" if branch and branch != "0" else ""),
            "debit": debit, "credit": credit, "balance": balance,
            "desc": [desc] if desc else [],
        }

    @staticmethod
    def _parse_anchor_tail(rest: str) -> tuple:
        """Split the line tail into (branch, ref, description, debit, credit, balance).

        The last three money tokens are Withdraws, Deposit, Balance positionally;
        the leading pure-digit tokens are Branch then (optionally) Ref/Chq; a
        ``0.00`` withdraw or deposit column means that side is empty.
        """
        tokens = rest.split()
        money_idx = [k for k, t in enumerate(tokens) if _MONEY.match(t)]
        if len(money_idx) < 3:
            return "", "", rest.strip(), None, None, None

        d_i, c_i, b_i = money_idx[-3], money_idx[-2], money_idx[-1]
        withdraws = clean_amount(tokens[d_i])[0]
        deposit = clean_amount(tokens[c_i])[0]
        balance = clean_amount(tokens[b_i])[0]

        head = tokens[:d_i]
        branch = ref = ""
        if head and head[0].isdigit():
            branch, head = head[0], head[1:]
        if head and head[0].isdigit():
            ref, head = head[0], head[1:]
        desc = " ".join(head)

        debit = None if (withdraws is not None and abs(withdraws) < 1e-9) else withdraws
        credit = None if (deposit is not None and abs(deposit) < 1e-9) else deposit
        return branch, ref, desc, debit, credit, balance

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
            "ref_no": current["ref_no"],
            "debit": current["debit"],
            "credit": current["credit"],
            "balance": current["balance"],
        })
