"""
stacked_header_test.py
======================
Regression test for stacked (two physical line) column headers.

Two scenarios, both drawn with reportlab canvas (absolutely positioned text):

  A. "Post Date" + Particulars/Chq/Debit/Credit/Balance on line 1, with a second
     "Date" stacked on the line below at the value-date x-position. Line 1 passes
     the header test on its own, but the merge must still pull in the stacked
     "Date" so it becomes the Value Date column.

  B. A header split so that NEITHER physical line passes the header test alone
     (line 1 = Post Date / Value Date / Cheque No / Particulars, no money; line 2
     = Debit / Credit / Balance, no date/particulars). This reproduces the
     reported bug (self.columns stays None -> zero transactions) and proves the
     stacked-header merge now recovers it.

Confirms for both: transactions are extracted, the second date maps to Value
Date, Chq.No values land in Cheque/Ref No (not Particulars), and the opening
"Brought Forward" balance is captured.
"""
import os
import tempfile

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from extractor import extract_pages
from normalizer import StatementNormalizer
from validator import validate, OK

PAGE_W, PAGE_H = A4
FAIL = []


def expect(label, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        FAIL.append(label)


def parse(path):
    norm = StatementNormalizer()
    raw_lines = []
    for page in extract_pages(path):
        raw_lines.append(page.lines)
        norm.feed_page(page)
    out = norm.finalize()
    validate(out, norm.opening_balance)
    return norm, out, raw_lines


# UTIB = Axis Bank IFSC prefix; Google India NEFT credits/debits.
TXNS = [
    ("02/04/24", "03/04/24", ["NEFT-UTIB0000123-GOOGLE INDIA",
                              "PVT LTD-VENDOR PAYOUT APR"], "100231", 45000.00, None),
    ("06/04/24", "06/04/24", ["NEFT-UTIB0000456-GOOGLE",
                              "ADSENSE CREDIT Q1"], "", None, 88250.75),
    ("11/04/24", "12/04/24", ["IMPS-UTIB0000789-REFUND",
                              "GOOGLE CLOUD"], "100232", None, 1200.00),
]
BF = 175000.00


def running(rows):
    bal = BF
    out = []
    for d, vd, desc, chq, dr, cr in rows:
        bal = bal - (dr or 0) + (cr or 0)
        out.append((d, vd, desc, chq, dr, cr, round(bal, 2)))
    return out


def _common_body(c, header_drawer, x):
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, PAGE_H - 38, "AXIS BANK")
    c.setFont("Helvetica", 8)
    c.drawString(40, PAGE_H - 52, "Statement of Account   A/c: 9111XXXXXX   IFSC: UTIB0000999")
    y = PAGE_H - 88
    y = header_drawer(c, y)
    # Brought forward (balance only)
    c.setFont("Helvetica", 8)
    c.drawString(x["date"], y, "01/04/24")
    c.drawString(x["vdate"], y, "01/04/24")
    c.drawString(x["part"], y, "BROUGHT FORWARD")
    c.drawRightString(x["bal"], y, f"{BF:,.2f}")
    y -= 16
    for d, vd, desc, chq, dr, cr, rbal in running(TXNS):
        c.setFont("Helvetica", 8)
        c.drawString(x["date"], y, d)
        c.drawString(x["vdate"], y, vd)
        c.drawString(x["part"], y, desc[0])
        if chq:
            c.drawString(x["chq"], y, chq)
        if dr is not None:
            c.drawRightString(x["dr"], y, f"{dr:,.2f}")
        if cr is not None:
            c.drawRightString(x["cr"], y, f"{cr:,.2f}")
        c.drawRightString(x["bal"], y, f"{rbal:,.2f}")
        y -= 12
        for cont in desc[1:]:
            c.drawString(x["part"], y, cont)
            y -= 12
        y -= 4
    c.save()


def scenario_a(path):
    """Line 1 is a full header; a second 'Date' is stacked below at value-date x."""
    x = dict(date=40, vdate=100, part=165, chq=325, dr=435, cr=505, bal=578)

    def header(c, y):
        c.setFont("Helvetica-Bold", 8)
        c.drawString(x["date"], y, "Post Date")
        c.drawString(x["part"], y, "Particulars")
        c.drawString(x["chq"], y, "Chq.No")
        c.drawRightString(x["dr"], y, "Debit")
        c.drawRightString(x["cr"], y, "Credit")
        c.drawRightString(x["bal"], y, "Balance")
        # stacked second date, one line below, at the value-date column x
        c.drawString(x["vdate"], y - 9, "Date")
        return y - 24

    c = canvas.Canvas(path, pagesize=A4)
    _common_body(c, header, x)


def scenario_b(path):
    """Header split so NEITHER physical line passes the header test alone."""
    x = dict(date=40, vdate=105, part=165, chq=330, dr=435, cr=505, bal=578)

    def header(c, y):
        c.setFont("Helvetica-Bold", 8)
        # line 1: dates + cheque + particulars, NO money
        c.drawString(x["date"], y, "Post Date")
        c.drawString(x["vdate"], y, "Value Date")
        c.drawString(x["part"], y, "Particulars")
        c.drawString(x["chq"], y, "Cheque No")
        # line 2 (stacked): money only, NO date/particulars
        c.drawRightString(x["dr"], y - 9, "Debit")
        c.drawRightString(x["cr"], y - 9, "Credit")
        c.drawRightString(x["bal"], y - 9, "Balance")
        return y - 24

    c = canvas.Canvas(path, pagesize=A4)
    _common_body(c, header, x)


def check_outcomes(tag, norm, out):
    expect(f"[{tag}] 3 transactions extracted (not zero)", len(out) == 3)
    if len(out) != 3:
        return
    expect(f"[{tag}] opening BROUGHT FORWARD captured", abs((norm.opening_balance or 0) - BF) < 0.01)
    expect(f"[{tag}] Value Date column captured", all(t["value_date"] is not None for t in out))
    expect(f"[{tag}] second date != first where expected",
           out[0]["date"] != out[0]["value_date"])
    expect(f"[{tag}] cheque in ref column", out[0]["ref_no"].replace(" ", "") == "100231")
    expect(f"[{tag}] cheque NOT in particulars", "100231" not in out[0]["particulars"])
    expect(f"[{tag}] multi-line particulars merged in order",
           out[0]["particulars"] == "NEFT-UTIB0000123-GOOGLE INDIA PVT LTD-VENDOR PAYOUT APR")
    expect(f"[{tag}] no cross-transaction leak", "ADSENSE" not in out[0]["particulars"])
    expect(f"[{tag}] debit/credit split", out[0]["debit"] == 45000.00 and out[1]["credit"] == 88250.75)
    expect(f"[{tag}] first row validates OK (opening seeded)", out[0]["validation"] == OK)


def run():
    # Scenario A
    pa = os.path.join(tempfile.gettempdir(), "stacked_a.pdf")
    scenario_a(pa)
    na, oa, _ = parse(pa)
    print("== Scenario A: stacked second 'Date' under 'Post Date' ==")
    for t in oa:
        print(f"  D={t['date'].strftime('%d-%m-%y') if t['date'] else '--'} "
              f"V={t['value_date'].strftime('%d-%m-%y') if t['value_date'] else '--'} "
              f"chq={t['ref_no']!r} {t['debit']}/{t['credit']} B={t['balance']} | {t['particulars']}")
    check_outcomes("A", na, oa)

    # Scenario B — first prove neither physical header line passes alone.
    pb = os.path.join(tempfile.gettempdir(), "stacked_b.pdf")
    scenario_b(pb)
    nb, ob, raw_lines = parse(pb)
    print("\n== Scenario B: header where neither line passes alone (zero-rows bug) ==")
    page0 = raw_lines[0]
    header_lines = [ln for ln in page0
                    if StatementNormalizer._header_fields(ln)]
    neither = (len(header_lines) >= 2
               and not StatementNormalizer._looks_like_header_words(header_lines[0])
               and not StatementNormalizer._looks_like_header_words(header_lines[1]))
    expect("[B] neither physical header line passes strict alone (bug condition)", neither)
    for t in ob:
        print(f"  D={t['date'].strftime('%d-%m-%y') if t['date'] else '--'} "
              f"V={t['value_date'].strftime('%d-%m-%y') if t['value_date'] else '--'} "
              f"chq={t['ref_no']!r} {t['debit']}/{t['credit']} B={t['balance']} | {t['particulars']}")
    check_outcomes("B", nb, ob)

    print()
    if FAIL:
        print(f"STACKED-HEADER TEST FAILED — {len(FAIL)} check(s): {FAIL}")
        return 1
    print("ALL STACKED-HEADER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
