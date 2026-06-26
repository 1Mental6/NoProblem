"""
idbi_parser.py
==============
Dedicated streaming engine for **IDBI Bank** "Customer Account Ledger" (REP31)
statements.

This is a fixed-width ledger whose empty Debit/Credit column simply VANISHES in
text extraction, so every transaction line carries only TWO money tokens -- the
transaction amount and the running balance (the balance keeps a ``Cr``/``Dr``
suffix). There is no ``0.00`` placeholder, no dash, and no per-amount Dr/Cr tag::

    GL.Date    Value Date Tran Id   Instrmnt Particulars                 Amount     Balance
    04-04-2025 04-04-2025 S28797974          UPI/546001833837/ASHOK SAH  290.00     23,204.59Cr
    05-04-2025 05-04-2025 S34248905          UPI/102627827535/ANKIT BISWAS 15,000.00 31,554.59Cr
    02-04-2025 02-04-2025 S4965965  288170   NEFT-OUTWARD-DR-288170      65,200.00  8,899.59Cr

Therefore **debit vs credit is decided purely by the balance direction**: if the
running balance fell, the amount was a debit; if it rose, a credit. The captured
``Opening Balance`` seeds the comparison for the first row.

Per line, after the two ``DD-MM-YYYY`` dates: a Tran Id (``S``/``M`` + digits) ->
Cheque/Ref No; an OPTIONAL instrument number (digits) -> also Cheque/Ref No; then
the Particulars; then the amount and the ``...Cr``/``...Dr`` balance. Column
artefacts such as a stray ``WBIN`` are non-numeric, so they stay in Particulars.

``Opening Balance : <amt>Cr`` (metadata) is the opening balance (not a row). The
repeated page header (timestamp / ``IDBI BANK LTD`` / ``Page N`` / ``REP31`` /
the two-line column header), the ``B/F Balance`` carry line, the ``Page Total
Debit/Credit`` lines, separators and account metadata are all skipped -- anything
that is not a ``date date TranId`` line is not a transaction.
"""

from __future__ import annotations

import re
from typing import List, Optional

from normalizer import clean_amount, parse_date

_DATE = r"\d{1,2}-\d{1,2}-\d{2,4}"            # DD-MM-YYYY (numeric month)
# Tran Id: a letter followed by digits. Mostly "S"/"M", but other prefixes occur
# (e.g. "C94736594"), so accept any leading letter rather than just S/M.
_TRANID = r"[A-Za-z]\d{3,}"
_TXN_START = re.compile(rf"^\s*({_DATE})\s+({_DATE})\s+({_TRANID})\b\s*(.*)$")
_MONEY = re.compile(r"^-?[\d,]+\.\d{1,2}(?:Cr|Dr)?$", re.I)
_OPENING = re.compile(r"opening\s+balance\s*:", re.I)


def detect_confidence(text_lines: List[str]) -> float:
    """Confidence that *text_lines* are an IDBI REP31 ledger (for the registry)."""
    up = [(ln or "").upper() for ln in text_lines]
    has_idbi = any("IDBI BANK" in ln for ln in up)
    has_ledger = any("CUSTOMER ACCOUNT LEDGER" in ln or "REP31" in ln for ln in up)
    if has_idbi and has_ledger:                # unique REP31 ledger signature
        return 0.99
    # Fallback when only a transaction page is seen (metadata page missed):
    has_header = any("TRAN ID" in ln and ("DEBIT AMOUNT" in ln or "TRANSACTION" in ln) for ln in up)
    has_txn = any(_TXN_START.match(ln or "") for ln in text_lines)
    if has_header and has_txn:
        return 0.97
    return 0.0


def _amount_with_sign(token: str) -> Optional[float]:
    """``clean_amount`` of a ``...Cr``/``...Dr`` balance, signed (Dr -> negative)."""
    value, drcr = clean_amount(token)
    if value is None:
        return None
    return -value if drcr == "DR" else value


class IDBIStatementParser:
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
        for pl in self._lines:
            text = pl.text.strip()
            if _OPENING.search(text):
                amt = self._trailing_amount(text)
                if amt is not None and self.opening_balance is None:
                    self.opening_balance = amt
                    self.prev_balance = amt
                continue
            start = _TXN_START.match(text)
            if not start:                       # header / metadata / footer -> skip
                continue
            txn = self._parse_transaction(start)
            if txn is not None:
                self.transactions.append(txn)
        return self.transactions

    @staticmethod
    def _trailing_amount(text: str) -> Optional[float]:
        money = [t for t in text.split() if _MONEY.match(t)]
        return _amount_with_sign(money[-1]) if money else None

    def _parse_transaction(self, start) -> Optional[dict]:
        date1 = parse_date(start.group(1))
        date2 = parse_date(start.group(2))
        tran_id = start.group(3)

        tokens = start.group(4).split()
        money_idx = [k for k, t in enumerate(tokens) if _MONEY.match(t)]
        if len(money_idx) < 2:                  # need amount + balance
            return None
        a_i, b_i = money_idx[-2], money_idx[-1]
        amount = clean_amount(tokens[a_i])[0]
        balance = _amount_with_sign(tokens[b_i])
        if balance is None:
            return None

        head = tokens[:a_i]
        instrmnt = ""
        if head and head[0].isdigit():          # optional instrument number
            instrmnt, head = head[0], head[1:]
        description = " ".join(head)

        # Debit vs credit purely from the running-balance movement.
        debit = credit = None
        if self.prev_balance is not None and amount is not None:
            if balance < self.prev_balance - 1e-6:
                debit = amount
            elif balance > self.prev_balance + 1e-6:
                credit = amount
        self.prev_balance = balance
        self.closing_balance = balance

        ref_no = " ".join(x for x in (tran_id, instrmnt) if x)
        return {
            "date": date1, "value_date": date2,
            "particulars": description, "ref_no": ref_no,
            "debit": debit, "credit": credit, "balance": balance,
        }
