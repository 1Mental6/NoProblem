"""Regression tests for the multi-format line parser (no real PDF needed).

Covers all THREE layouts the parser must auto-detect per statement:

  * the dash-delimited SBI layout  ``... Ref Debit Credit Balance`` where empty
    columns appear literally as ``-`` and must be read POSITIONALLY,
  * the original no-dash Indian Bank layout ``... <amount> <balance>Cr|Dr`` whose
    direction comes from the running-balance movement / Cr|Dr suffix, and
  * the single-amount + CR|DR HDFC/Axis layout ``... <amount> <CR|DR> <balance>``
    with a leading S.NO, trailing branch code, and split NEFT/RTGS rows whose
    amount sits on a different physical line from the S.NO+date anchor.

The Indian Bank PDF is not shipped in this repo, so the documented Indian Bank
line (and its both-directions wrapped description) is reproduced here with
synthetic word geometry to prove that code path was not disturbed by later fixes.
"""
from extractor import Word
from line_parser import LineStatementParser, pline_from_words, PLine


def check(label, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    assert cond, label


def words_line(text, top, right_x1):
    """A description PLine carrying one word box ending at *right_x1*."""
    return PLine(text, float(top), [Word(text, 50.0, float(right_x1), float(top), float(top) + 8)])


def dated_line(tokens_with_x, top):
    """A dated PLine built from explicit (text, x0, x1) word boxes."""
    ws = [Word(t, float(x0), float(x1), float(top), float(top) + 8) for t, x0, x1 in tokens_with_x]
    return pline_from_words(ws)


# ===========================================================================
# 1. dash-tail positional parsing (the SBI core fix)
# ===========================================================================
print("== SBI dash-delimited positional parsing ==")
p = LineStatementParser()

# Ref=-, Debit=-, Credit=25,000.00, Balance=26,606.06  (a credit)
credit_line = dated_line([
    ("04/04/2024", 27, 67), ("04/04/2024", 82, 122),
    ("CDM4040109MAL", 138, 210), ("BAZAR", 214, 250),
    ("-", 303, 306), ("-", 350, 353), ("25,000.00", 400, 460), ("26,606.06", 500, 560),
], top=100)
parsed = p._parse_dated(credit_line)
check("credit line detected as dash_format", parsed["dash_format"] is True)
check("credit -> credit column", parsed["credit"] == 25000.0)
check("credit line debit is empty (dash -> None)", parsed["debit"] is None)
check("credit line ref empty (dash -> '')", parsed["ref"] == "")
check("credit line balance", parsed["balance"] == 26606.06)
check("inline description kept (no dashes/amounts)", parsed["inline"] == "CDM4040109MAL BAZAR")

# Ref=-, Debit=20,000.00, Credit=-, Balance=6,606.06  (a debit, empty inline)
debit_line = dated_line([
    ("04/04/2024", 27, 67), ("04/04/2024", 82, 122),
    ("-", 303, 306), ("20,000.00", 350, 410), ("-", 446, 449), ("6,606.06", 514, 560),
], top=140)
parsed = p._parse_dated(debit_line)
check("debit -> debit column", parsed["debit"] == 20000.0)
check("debit line credit empty (dash -> None)", parsed["credit"] is None)
check("debit line empty inline description", parsed["inline"] == "")

# value-date-first: first leading date -> value_date, second -> posting date.
check("value_date = first leading date", parsed["value_date"].strftime("%d/%m/%Y") == "04/04/2024")
check("date = second leading date", parsed["date"].strftime("%d/%m/%Y") == "04/04/2024")

# An account number with no decimal must NOT be mistaken for a money column.
acct_line = dated_line([
    ("05/04/2024", 27, 67), ("05/04/2024", 82, 122),
    ("TFR", 138, 160), ("0032035033755", 165, 250),
    ("-", 303, 306), ("-", 350, 353), ("1,000.00", 400, 460), ("2,606.06", 500, 560),
], top=180)
parsed = p._parse_dated(acct_line)
check("account number stays in description", parsed["inline"] == "TFR 0032035033755")
check("account-number line still credit 1,000.00", parsed["credit"] == 1000.0)


# ===========================================================================
# 2. no-dash (Indian Bank) line is NOT treated as dash format
# ===========================================================================
print("\n== Indian Bank no-dash auto-detection ==")
ib_line = dated_line([
    ("01/04/25", 27, 60), ("01/04/25", 70, 103),
    ("8050.00", 300, 340), ("119544.23Cr", 480, 545),
], top=100)
parsed = p._parse_dated(ib_line)
check("no-dash line NOT dash_format", parsed["dash_format"] is False)
check("no-dash amount captured", parsed["amount"] == 8050.0)
check("no-dash balance captured", parsed["balance"] == 119544.23)


# ===========================================================================
# 3. gap-split attachment prevents description bleed across transactions
# ===========================================================================
print("\n== gap-split attachment (no cross-transaction bleed) ==")
# Two SBI transactions; the first has TWO trailing description lines whose last
# line sits just past the midpoint to the second dated line. Nearest-line
# attachment would bleed it into txn #2; the gap split must keep it with txn #1.
sp = LineStatementParser(opening_balance=2306.06)
dated = [
    (dated_line([("02/04/2024", 27, 67), ("02/04/2024", 82, 122),
                 ("MoneyTRF", 138, 180), ("TXN", 184, 205),
                 ("-", 303, 306), ("-", 350, 353), ("20,000.00", 400, 460), ("22,306.06", 500, 560)],
                top=538.43)),
    (dated_line([("02/04/2024", 27, 67), ("02/04/2024", 82, 122),
                 ("OF", 138, 150), ("LOHAR", 224, 252),
                 ("-", 303, 306), ("20,700.00", 350, 410), ("-", 446, 449), ("1,606.06", 514, 560)],
                top=579.43)),
]
descs = [
    words_line("0038408454418 OF ASTHANA", 550.09, 249),
    words_line("RURAL DEVEP AN AT 02084 MAL", 559.40, 263),   # belongs to txn #1
    words_line("WDL TFR Joy 0020050391379", 572.12, 257),     # belongs to txn #2
]
parsed_dated = [(pl, sp._parse_dated(pl)) for pl in dated]
sp._emit(parsed_dated, descs)
t0, t1 = sp.transactions
print(f"  txn0: {t0['particulars']!r}")
print(f"  txn1: {t1['particulars']!r}")
check("txn0 keeps its own trailing 'RURAL DEVEP...'", "RURAL DEVEP AN AT 02084 MAL" in t0["particulars"])
check("txn1 does NOT inherit txn0's 'RURAL DEVEP'", "RURAL DEVEP" not in t1["particulars"])
check("txn1 keeps its own leading 'WDL TFR Joy'", "WDL TFR Joy 0020050391379" in t1["particulars"])
check("txn0 credit positional", t0["credit"] == 20000.0 and t0["debit"] is None)
check("txn1 debit positional", t1["debit"] == 20700.0 and t1["credit"] is None)


# ===========================================================================
# 4. Indian Bank both-directions wrap merge still reproduces the exact text
# ===========================================================================
print("\n== Indian Bank wrapped-description merge (mid-word + word-boundary) ==")
ib = LineStatementParser()
plines = [
    words_line("Brought Forward 111494.23Cr", 0, 260),
    words_line("TRANSFER FROM 97157057378", 10, 200),           # leading, not full
    words_line("/IMPS/P2A/509201599397/ /IMPS/GOO", 20, 300),   # leading, FULL -> mid-word wrap
    dated_line([("01/04/25", 27, 60), ("01/04/25", 70, 103),
                ("8050.00", 300, 340), ("119544.23Cr", 480, 545)], top=30),
    words_line("GLEINDIAD /BRANCH : ATM SERVICE", 40, 250),     # trailing, not full
    words_line("BRANCH", 50, 100),                              # trailing
]
ib.feed_page(plines)
txn = ib.transactions[0]
expected = ("TRANSFER FROM 97157057378 /IMPS/P2A/509201599397/ "
            "/IMPS/GOOGLEINDIAD /BRANCH : ATM SERVICE BRANCH")
print(f"  particulars: {txn['particulars']!r}")
check("opening balance captured from Brought Forward", ib.opening_balance == 111494.23)
check("Indian Bank credit inferred from balance rise", txn["credit"] == 8050.0)
check("Indian Bank balance", txn["balance"] == 119544.23)
check("Indian Bank wrapped particulars exact (mid-word glued, words spaced)",
      txn["particulars"] == expected)


# ===========================================================================
# 5. HDFC / Axis single-amount + CR|DR indicator (incl. the split-line variant)
# ===========================================================================
print("\n== HDFC single-amount + CR|DR (split-line association) ==")
hd = LineStatementParser()
hd.feed_page([
    PLine("Opening Balance: INR 12,07,458.10", 10),
    # split NEFT: money line ABOVE its S.NO anchor, fragment BELOW
    PLine("NEFT/HDFCH00158751807/SBI MUTUAL 25,500.00 CR 12,32,958.10 SILIGURI [WB] (248)", 20),
    PLine("1 03/04/2025 03/04/2025 FUND/HDFC BANK/0001NEFT", 21),
    PLine("00600350000549", 22),
    # single-line debit, with trailing branch/SOL code to strip
    PLine("2 19/04/2025 19/04/2025 Monthly Service Chrgs 100.00 DR 12,32,858.10 SILIGURI [WB] (035)", 23),
    # garbled totals footer -> must be skipped
    PLine("3 TRANSACTION TOTAL DR/CR 23,85 , , 7 9 8 5 SILIGURI [WB]", 24),
])
t = hd.transactions
check("HDFC opening balance captured", hd.opening_balance == 1207458.10)
check("HDFC parsed 2 transactions (TRANSACTION TOTAL skipped)", len(t) == 2)
check("HDFC split NEFT -> credit via CR indicator", t[0]["credit"] == 25500.0 and t[0]["debit"] is None)
check("HDFC split NEFT balance", t[0]["balance"] == 1232958.10)
check("HDFC split description assembled across lines in order",
      t[0]["particulars"] == "NEFT/HDFCH00158751807/SBI MUTUAL FUND/HDFC BANK/0001NEFT 00600350000549")
check("HDFC single-line -> debit via DR indicator", t[1]["debit"] == 100.0 and t[1]["credit"] is None)
check("HDFC branch/SOL code stripped from particulars", t[1]["particulars"] == "Monthly Service Chrgs")
check("HDFC S.NO stripped, dates captured", t[1]["date"].strftime("%d/%m/%Y") == "19/04/2025")

# auto-detection must NOT misfire on the other two formats
check("HDFC detector ignores SBI dash line",
      LineStatementParser._is_hdfc_page([PLine("04/04/2024 04/04/2024 X - - 25,000.00 26,606.06", 1)]) is False)
check("HDFC detector ignores Indian Bank line",
      LineStatementParser._is_hdfc_page([PLine("01/04/25 01/04/25 8050.00 119544.23Cr", 1)]) is False)
check("HDFC detector fires on CR-between-amounts line",
      LineStatementParser._is_hdfc_page([PLine("X 100.00 DR 200.00 BR [WB]", 1)]) is True)

print("\nALL DASH-FORMAT TESTS PASSED")
