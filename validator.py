"""
validator.py
============
Integrity check for the normalized transactions.

For each row we expect::

    previous_balance + credit - debit == current_balance   (within tolerance)

The result goes into a ``validation`` field as ``"OK"`` or ``"CHECK"`` so the
user can instantly spot extraction errors in the Excel output.

The statement's opening ("Brought Forward") balance is used as the
``previous_balance`` for the very first real transaction, so that first row
validates as ``OK`` rather than being unverifiable. A row with no balance, or the
first row when no opening balance was found, is marked ``"—"`` (cannot verify).
"""

from __future__ import annotations

from typing import List, Optional

TOLERANCE = 0.01      # ±0.01 rounding tolerance, per spec

OK = "OK"
CHECK = "CHECK"
UNVERIFIED = "—"


def validate(transactions: List[dict], opening_balance: Optional[float] = None) -> List[dict]:
    """Annotate each transaction with a ``validation`` note. Mutates in place."""
    prev_balance = opening_balance

    for txn in transactions:
        debit = txn.get("debit") or 0.0
        credit = txn.get("credit") or 0.0
        balance = txn.get("balance")

        if balance is None:
            txn["validation"] = UNVERIFIED
            if prev_balance is not None:
                prev_balance = prev_balance + credit - debit
            continue

        if prev_balance is None:
            # No opening balance to compare the first row against.
            txn["validation"] = UNVERIFIED
            prev_balance = balance
            continue

        expected = prev_balance + credit - debit
        txn["validation"] = OK if abs(expected - balance) <= TOLERANCE else CHECK
        prev_balance = balance

    return transactions


def validation_summary(transactions: List[dict]) -> dict:
    """Counts for status messages / dialogs."""
    total = len(transactions)
    checks = sum(1 for t in transactions if t.get("validation") == CHECK)
    return {"total": total, "checks": checks, "ok": total - checks}
