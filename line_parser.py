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

Two trailing-column layouts are auto-detected, per line, per statement
-------------------------------------------------------------------------
1. **No-dash (e.g. Indian Bank)** -- the dated line ends with just
   ``<amount> <balance>Cr|Dr``; direction is taken from the ``Cr``/``Dr`` suffix
   or, failing that, from the running-balance movement. This is the original
   path and is left unchanged.

2. **Dash-delimited (e.g. SBI)** -- after the two leading dates and the inline
   description, the line ends with a FIXED sequence of four positional columns::

       Ref | Debit | Credit | Balance

   where any empty column appears literally as a single dash ``-``::

       04/04/2024 04/04/2024 CDM4040109MAL BAZAR - - 25,000.00 26,606.06
                                                  ^ ^ ^^^^^^^^^ ^^^^^^^^^
                                                ref deb  credit  balance

   The trailing block is read **positionally** (token[0]=ref, [1]=debit,
   [2]=credit, [3]=balance); a ``-`` means that column is empty/None and a number
   means it holds that value. Debit vs credit comes DIRECTLY from those columns,
   never from a balance comparison. We anchor the block to the END of the line
   (the last four dash-or-number tokens) so an account number or a stray ``-``
   inside the description cannot be mistaken for a column. SBI's column order is
   Value Date | Post Date, so of the two leading dates the FIRST is the value
   date and the SECOND is the posting date.

   Detection is automatic: a line is parsed in dash mode only when its last four
   tokens are all dash-or-number AND at least one of the Ref/Debit/Credit tokens
   is a standalone ``-``. Statements without such dashes fall through to path 1,
   so both formats work with no manual switch.

3. **Single-amount + CR|DR indicator (e.g. HDFC / Axis)** -- handled by a
   separate page path (:meth:`LineStatementParser._feed_page_hdfc`). One Amount
   column carries a separate Debit/Credit indicator BETWEEN the amount and the
   running balance::

       23 20/09/2025 20/09/2025 Monthly Service Chrgs 100.00 DR 23,28,470.10 SILIGURI [WB] (035)
       ^SNO ^txn date ^value dt ^description           ^amt   ^ind ^balance   ^branch (dropped)

   The amount is routed by the indicator (CR -> Credit, DR -> Debit), the leading
   S.NO is stripped, and the trailing branch/SOL code -- being positionally AFTER
   the balance -- never enters the description. NEFT/RTGS rows are SPLIT across
   physical lines: the ``... amount CR balance`` money line sits just above its
   ``S.NO date date <desc>`` anchor, with more description fragments below; these
   are reassembled in reading order into one transaction. A garbled
   ``... TRANSACTION TOTAL ...`` row is recognised as a footer and skipped.

   Detection is automatic and sticky per statement: a page showing the
   ``<amount> <CR|DR> <balance>`` signature switches to this path.

Markers:
    "Brought Forward <amt>Cr"  -> opening balance (seed prev_balance; not a txn)
    "Carried Forward <amt>Cr"  -> page carry / closing marker (not a txn)
    "Closing Balance : <amt>"  -> closing balance (not a txn)
    Summary / "Dr. Count.." / disclaimer / page metadata -> skipped.

Cheque/reference numbers are embedded in the description for the no-dash format
and are intentionally left inside Particulars (no separate ref column); for the
dash-delimited format the explicit Ref column is captured into ``ref_no``.
"""

from __future__ import annotations

import re
from typing import List, Optional

from normalizer import clean_amount, parse_date, _MONEY_TOKEN_RE

# --- line / token patterns --------------------------------------------------
_LEAD_DATE = re.compile(r"^\s*(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})")
_WORD_DATE = re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$")
_WORD_MONEY = re.compile(r"^[₹]?[\d,]+\.\d{1,2}(?:cr|dr)?$", re.I)

# A single placeholder dash standing in for an empty column (SBI-style).
_DASH = "-"

# How many trailing positional columns the dash-delimited format carries:
#   Ref | Debit | Credit | Balance
_DASH_TAIL_LEN = 4


def _is_dash(tok: str) -> bool:
    return tok == _DASH


def _is_money_word(tok: str) -> bool:
    return _WORD_MONEY.match(tok) is not None

_OPENING = re.compile(r"brought\s+forward", re.I)
_CLOSING = re.compile(r"carried\s+forward|closing\s+balance", re.I)
_FOOTER = re.compile(
    r"\b(statement|summary)\b|dr\.?\s*count|cr\.?\s*count"
    r"|in\s+case\s+your\s+account|transaction\s+with\s+extra\s+care|page\s*no",
    re.I,
)

# --- HDFC / Axis single-amount + CR|DR indicator format ---------------------
# Signature: ONE amount column with a separate Debit/Credit indicator sitting
# BETWEEN the amount and the running balance -> "<amount> <CR|DR> <balance>".
_HDFC_MONEY = re.compile(r"([\d,]+\.\d{1,2})\s+\b(CR|DR)\b\s+([\d,]+\.\d{1,2})", re.I)
# A transaction anchor line: a leading S.NO integer then the two dates.
_HDFC_ANCHOR = re.compile(
    r"^\s*(\d+)\s+(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\s+(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\s*(.*)$"
)
_HDFC_TOTAL = re.compile(r"transaction\s+total", re.I)
_HDFC_OPENING = re.compile(r"opening\s+balance", re.I)
_HDFC_CLOSING = re.compile(r"closing\s+balance", re.I)

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


def page_to_plines(page) -> List[PLine]:
    """Convert one extractor ``PageData`` into positioned lines for the parser.

    Coordinate words become geometry-aware ``PLine``s; OCR / plain-text pages get
    a synthetic ``top`` from their reading order so vertical-order logic still works.
    """
    if page.lines:                                   # coordinate words available
        return [pline_from_words(line) for line in page.lines if line]
    if page.text_lines:                              # OCR / plain text: synth y = index
        return [PLine(t, float(i)) for i, t in enumerate(page.text_lines) if t.strip()]
    return []


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

    def __init__(self, opening_balance: Optional[float] = None,
                 force_format: Optional[str] = None) -> None:
        self.transactions: List[dict] = []
        self.opening_balance = opening_balance
        self.closing_balance: Optional[float] = None
        self.prev_balance = opening_balance
        self.detected_format: Optional[str] = None   # locked once a page is classified
        # When a caller already knows the format (a specific bank was chosen) it
        # can pin the path and skip per-page auto-detection:
        #   "hdfc" -> single-amount + CR|DR path
        #   "line" -> the SBI-dash / Indian-Bank line path (dash vs no-dash still
        #             auto-resolves per line within that path)
        #   None   -> auto-detect (default; original behaviour)
        self.force_format = force_format

    # -- public ------------------------------------------------------------
    def feed_page(self, plines: List[PLine]) -> None:
        if not plines:
            return

        # The HDFC/Axis single-amount + CR|DR layout is structurally different
        # (split money lines, S.NO anchors), so it gets its own path. Detection is
        # sticky: once a statement shows the "<amount> <CR|DR> <balance>" signature
        # every later page (closing balance, disclaimers) stays on that path too.
        use_hdfc = self.force_format == "hdfc" or (
            self.force_format is None
            and (self.detected_format == "hdfc" or self._is_hdfc_page(plines)))
        if use_hdfc:
            self.detected_format = "hdfc"
            self._feed_page_hdfc(plines)
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

    # -- HDFC / Axis single-amount + CR|DR path ----------------------------
    @staticmethod
    def _is_hdfc_page(plines: List[PLine]) -> bool:
        """True when a page shows the ``<amount> <CR|DR> <balance>`` signature."""
        return any(_HDFC_MONEY.search(pl.text) for pl in plines)

    def _feed_page_hdfc(self, plines: List[PLine]) -> None:
        """Parse a single-amount + CR|DR statement page (HDFC/Axis style).

        Each transaction is anchored by a line that starts with ``S.NO date date``.
        The amount/indicator/balance may be ON that anchor line (single-line
        transaction) or on a SEPARATE money line that sits immediately above it
        (the split NEFT/RTGS variant), with extra description fragments below.
        Amount direction is taken straight from the CR/DR indicator. Branch/SOL
        codes trail the balance positionally, so they never enter the description.
        """
        plines = sorted(plines, key=lambda p: p.top)

        # Opening / closing balance markers can appear on any page.
        for pl in plines:
            if _HDFC_OPENING.search(pl.text):
                amt = _last_money(pl.text)
                if amt is not None and self.opening_balance is None:
                    self.opening_balance = amt
                    self.prev_balance = amt
            if _HDFC_CLOSING.search(pl.text):
                amt = _last_money(pl.text)
                if amt is not None:
                    self.closing_balance = amt

        n = len(plines)
        consumed = [False] * n
        i = 0
        while i < n:
            text = plines[i].text
            if _HDFC_TOTAL.search(text):          # garbled "TRANSACTION TOTAL" row
                i += 1
                continue
            anc = _HDFC_ANCHOR.match(text)
            if not anc:                            # money-only / fragment / preamble
                i += 1
                continue

            date1, date2 = parse_date(anc.group(2)), parse_date(anc.group(3))
            rest = anc.group(4)
            desc_parts: List[str] = []
            amount = indicator = balance = None

            money = _HDFC_MONEY.search(rest)
            if money is not None:
                # Single-line transaction: amount/indicator/balance are inline.
                desc_parts.append(rest[:money.start()])
                amount, indicator, balance = self._hdfc_money(money)
            else:
                # Split transaction: the money line is the one directly above,
                # and its description fragment comes FIRST in reading order.
                if i - 1 >= 0 and not consumed[i - 1]:
                    pm = _HDFC_MONEY.search(plines[i - 1].text)
                    if pm is not None and not _HDFC_ANCHOR.match(plines[i - 1].text):
                        desc_parts.append(plines[i - 1].text[:pm.start()])
                        amount, indicator, balance = self._hdfc_money(pm)
                        consumed[i - 1] = True
                desc_parts.append(rest)

            # Trailing description fragments below the anchor, up to the next
            # transaction (anchor), the next money line, or the totals row.
            j = i + 1
            while j < n:
                t = plines[j].text
                if _HDFC_ANCHOR.match(t) or _HDFC_MONEY.search(t) or _HDFC_TOTAL.search(t):
                    break
                desc_parts.append(t)
                consumed[j] = True
                j += 1

            if balance is not None:
                particulars = re.sub(r"\s{2,}", " ", " ".join(p.strip() for p in desc_parts if p.strip())).strip()
                self.prev_balance = balance
                self.transactions.append({
                    "date": date1, "value_date": date2,
                    "particulars": particulars, "ref_no": "",
                    "debit": amount if indicator == "DR" else None,
                    "credit": amount if indicator == "CR" else None,
                    "balance": balance,
                })
            i = j

    @staticmethod
    def _hdfc_money(match) -> tuple:
        """``(amount, 'CR'|'DR', balance)`` from an ``_HDFC_MONEY`` match."""
        amount = clean_amount(match.group(1))[0]
        balance = clean_amount(match.group(3))[0]
        return amount, match.group(2).upper(), balance

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

        # --- dash-delimited (SBI) trailing block: read positionally ---------
        dashed = self._parse_dash_tail(rest)
        if dashed is not None:
            inline, ref, debit, credit, balance = dashed
            # SBI column order is Value Date | Post Date: the first leading date
            # is the value date, the second is the posting date. Fall back to the
            # single date for both display columns when only one is present.
            if date2 is not None:
                tx_date, value_date = date2, date1
            else:
                tx_date, value_date = date1, None
            return {"dash_format": True,
                    "date": tx_date, "value_date": value_date,
                    "balance": balance, "ref": ref,
                    "debit": debit, "credit": credit,
                    "inline": inline, "inline_x1": self._inline_right_edge(pl)}

        # --- no-dash (Indian Bank) trailing block: last two numbers ---------
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

        return {"dash_format": False,
                "date": date1, "value_date": date2, "balance": balance,
                "amount": amount, "amount_drcr": a_drcr,
                "inline": inline, "inline_x1": self._inline_right_edge(pl)}

    @staticmethod
    def _parse_dash_tail(rest: str):
        """Read an SBI-style ``Ref Debit Credit Balance`` trailing block.

        The block is anchored to the END of the line: the last four
        whitespace-separated tokens. It is accepted only when every one of those
        tokens is a standalone dash or a money number AND at least one of the
        Ref/Debit/Credit tokens is a dash placeholder -- the signal that
        distinguishes this layout from the no-dash format (whose tail is just
        ``<amount> <balance>``). Returns ``(inline, ref, debit, credit, balance)``
        with ``-`` columns mapped to ``None``, or ``None`` when not dash-delimited.
        """
        tokens = rest.split()
        if len(tokens) < _DASH_TAIL_LEN:
            return None
        tail = tokens[-_DASH_TAIL_LEN:]
        if not all(_is_dash(t) or _is_money_word(t) for t in tail):
            return None
        ref_tok, debit_tok, credit_tok, bal_tok = tail
        # Balance is always present; the dash signal must be in Ref/Debit/Credit.
        if not _is_money_word(bal_tok):
            return None
        if not any(_is_dash(t) for t in (ref_tok, debit_tok, credit_tok)):
            return None

        balance, b_drcr = clean_amount(bal_tok)
        if balance is None:
            return None
        if b_drcr == "DR":
            balance = -balance                 # overdrawn balance shown as "...Dr"
        debit = None if _is_dash(debit_tok) else clean_amount(debit_tok)[0]
        credit = None if _is_dash(credit_tok) else clean_amount(credit_tok)[0]
        ref = "" if _is_dash(ref_tok) else ref_tok
        inline = " ".join(tokens[:-_DASH_TAIL_LEN]).strip()
        return inline, ref, debit, credit, balance

    @staticmethod
    def _inline_right_edge(pl: PLine) -> Optional[float]:
        """Right edge of the inline description words (for wrap detection).

        Dates, money numbers and standalone dash placeholders are excluded so the
        edge reflects only real description text, never a trailing column token.
        """
        if not pl.words:
            return None
        desc_words = [w for w in pl.words
                      if not _WORD_MONEY.match(w.text)
                      and not _WORD_DATE.match(w.text)
                      and not _is_dash(w.text)]
        return max((w.x1 for w in desc_words), default=None)

    # -- assembly ----------------------------------------------------------
    def _emit(self, dated, descs) -> None:
        if not dated:
            return
        dated.sort(key=lambda d: d[0].top)
        tops = [d[0].top for d in dated]

        # Right edge of the description column on this page (for wrap detection).
        edge = max((pl.right_x1 for pl in descs if pl.right_x1 is not None), default=None)

        buckets = self._attach(tops, descs)

        for (pl, parsed), bucket in zip(dated, buckets):
            bucket.sort(key=lambda x: x.top)
            frags = []   # (text, is_full) in top-to-bottom order
            for d in bucket:
                if d.top < pl.top:                       # above -> leading
                    frags.append((d.text, self._is_full(d.right_x1, edge)))
            if parsed["inline"]:
                frags.append((parsed["inline"], self._is_inline_full(parsed["inline_x1"], edge)))
            for d in bucket:
                if d.top >= pl.top:                       # below -> trailing
                    frags.append((d.text, self._is_full(d.right_x1, edge)))

            particulars = self._assemble(frags)
            self.transactions.append(self._make_txn(parsed, particulars))

    @staticmethod
    def _attach(tops: List[float], descs: List[PLine]) -> List[List[PLine]]:
        """Assign each orphan description line to its owning dated line.

        Description lines wrap both ABOVE (leading) and BELOW (trailing) the dated
        line, with no textual delimiter between one transaction's trailing run and
        the next one's leading run. We keep each contiguous run intact by splitting
        the band between two consecutive dated lines at its **largest vertical
        gap** -- the blank space that separates one transaction's block from the
        next -- rather than at the midpoint. A pure nearest-dated-line rule pushes
        a transaction's last trailing line down into the following transaction
        whenever it happens to sit just past the midpoint; the gap split keeps it
        with its own block, so descriptions never bleed into a neighbour.

        Ties (e.g. evenly spaced OCR lines with synthetic y) break toward the
        midpoint, reproducing the old nearest-line behaviour where there is no
        distinguishing gap.
        """
        from bisect import bisect_right

        buckets: List[List[PLine]] = [[] for _ in tops]
        if not tops:
            return buckets

        # Group each description line under the dated line at or above it.
        segments: List[List[PLine]] = [[] for _ in tops]
        for dl in sorted(descs, key=lambda d: d.top):
            i = max(0, bisect_right(tops, dl.top) - 1)
            segments[i].append(dl)

        for i, run in enumerate(segments):
            if not run:
                continue
            if i == len(tops) - 1:           # below the last dated line -> trailing
                buckets[i].extend(run)
                continue
            split = LineStatementParser._split_run(tops[i], run, tops[i + 1])
            buckets[i].extend(run[:split])           # trailing of dated[i]
            buckets[i + 1].extend(run[split:])       # leading of dated[i+1]
        return buckets

    @staticmethod
    def _split_run(top_above: float, run: List[PLine], top_below: float) -> int:
        """Where to cut a run of description lines between two dated lines.

        Returns the index ``k`` such that ``run[:k]`` trail the upper dated line
        and ``run[k:]`` lead the lower one. The cut is placed at the largest gap
        in the vertical sequence ``[top_above, *run tops, top_below]``; on a tie
        it is placed closest to the middle of the run.
        """
        pts = [top_above] + [d.top for d in run] + [top_below]
        n = len(run)
        mid = n / 2.0
        best_k, best_gap, best_dist = 0, -1.0, 1e18
        for k in range(n + 1):
            gap = pts[k + 1] - pts[k]
            dist = abs(k - mid)
            if gap > best_gap + 1e-9 or (abs(gap - best_gap) <= 1e-9 and dist < best_dist):
                best_k, best_gap, best_dist = k, gap, dist
        return best_k

    @staticmethod
    def _is_full(x1: Optional[float], edge: Optional[float]) -> bool:
        """The fragment reached the column's right edge -> a word wrapped across."""
        return edge is not None and x1 is not None and x1 >= edge - _WRAP_TOL

    @staticmethod
    def _is_inline_full(x1: Optional[float], edge: Optional[float]) -> bool:
        """Wrap test for the inline description that sits ON the dated line.

        Unlike a wrapped orphan line, the inline description occupies a WIDER band
        (it flows rightward toward the amount columns), so its right edge normally
        sits well past ``edge`` -- the right boundary of the narrow wrapped-text
        column. Only treat it as a mid-word wrap when it ends *within* that column
        (``x1`` near ``edge``); an inline that overshoots ``edge`` ended at a normal
        word boundary and must be joined to the next fragment with a space.
        """
        return (edge is not None and x1 is not None
                and edge - _WRAP_TOL <= x1 <= edge + _WRAP_TOL)

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

        # Dash-delimited (SBI): debit/credit come straight from their own
        # positional columns -- never inferred from the balance movement.
        if parsed.get("dash_format"):
            self.prev_balance = balance
            return {
                "date": parsed["date"],
                "value_date": parsed["value_date"],
                "particulars": particulars,
                "ref_no": parsed.get("ref") or "",
                "debit": parsed["debit"],
                "credit": parsed["credit"],
                "balance": balance,
            }

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
