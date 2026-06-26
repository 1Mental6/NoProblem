"""Regression tests for the bank-parser registry, auto-detection and password flow.

Covers the new architecture only (no real statement PDF needed):
  * every required bank appears in the GUI dropdown choices,
  * each format's signature is detected by the right parser and by no other,
  * select_parser() honours an explicit bank and falls back to the flagged
    generic parser when auto-detection is not confident,
  * stub banks are registered, marked not-implemented, and delegate to generic,
  * (if PyMuPDF is available) the encrypted-PDF password helpers behave.
"""
import os
import tempfile

import bank_parsers as bp
from extractor import requires_password, password_is_correct, PasswordError


def check(label, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    assert cond, label


# Representative single lines for each supported format.
HDFC_LINE = "23 20/09/2025 20/09/2025 Monthly Service Chrgs 100.00 DR 23,28,470.10 SILIGURI [WB] (035)"
SBI_LINE = "04/04/2024 04/04/2024 CDM4040109MAL BAZAR - - 25,000.00 26,606.06"
IB_LINE = "01/04/25 01/04/25 8050.00 119544.23Cr"
BANDHAN_LINES = [
    "TRANS VALUE CHEQUE / DESCRIPTION DEBITS CREDITS BALANCE",
    "02-APR- 02-APR- 000005 CHQ PAID-TP-CW 90,000.00 0.00 12,607.00",
    "2025 2025 GUDDU MAJHI -",
]
CANARA_LINES = [
    "TRANS VALUE BRANCH REF/CHQ.NO DESCRIPTION WITHDRAWS DEPOSIT BALANCE",
    "04-APR-25 04-APR-25 6396 NON-SUBMISSION OF 10,000.00 0.00 -3,033,185.24",
]
IDBI_LINES = [
    "REP31 Customer Account Ledger Print",
    "25-05-2026 17:55:27 IDBI BANK LTD, SILIGURI Page 1",
    "Opening Balance : 50,549.59Cr",
    "05-04-2025 05-04-2025 S34555947 VISA-POS/BROADWAY HOTEL KOLKATTA IND 16,098.00 15,456.59Cr",
]
JUNK = ["Dear customer", "Welcome to your statement", "Total: many rupees"]


# --- 1. dropdown choices ----------------------------------------------------
print("== GUI bank choices ==")
labels = [lbl for _, lbl in bp.BANK_CHOICES]
keys = [k for k, _ in bp.BANK_CHOICES]
check("Auto-detect is first", bp.BANK_CHOICES[0] == ("auto", "Auto-detect"))
for required in ["HDFC", "ICICI", "Union Bank", "Canara Bank", "IDBI Bank",
                 "Bandhan Bank", "Indian Bank", "SBI"]:
    check(f"dropdown offers {required}", required in labels)
check("generic parser hidden from dropdown", "generic" not in keys)


# --- 2. detection is discriminative ----------------------------------------
print("\n== per-format detection ==")
check("HDFC parser detects HDFC line", bp.REGISTRY["hdfc"].detect([HDFC_LINE]) >= 0.5)
check("HDFC parser ignores SBI line", bp.REGISTRY["hdfc"].detect([SBI_LINE]) == 0.0)
check("HDFC parser ignores Indian Bank line", bp.REGISTRY["hdfc"].detect([IB_LINE]) == 0.0)

check("SBI parser detects SBI line", bp.REGISTRY["sbi"].detect([SBI_LINE]) >= 0.5)
check("SBI parser ignores HDFC line", bp.REGISTRY["sbi"].detect([HDFC_LINE]) == 0.0)
check("SBI parser ignores Indian Bank line", bp.REGISTRY["sbi"].detect([IB_LINE]) == 0.0)

check("Indian Bank parser detects IB line", bp.REGISTRY["indian_bank"].detect([IB_LINE]) >= 0.5)
check("Indian Bank parser ignores HDFC line", bp.REGISTRY["indian_bank"].detect([HDFC_LINE]) == 0.0)
check("Indian Bank parser ignores SBI line", bp.REGISTRY["indian_bank"].detect([SBI_LINE]) == 0.0)

check("Bandhan parser detects split-date lines", bp.REGISTRY["bandhan"].detect(BANDHAN_LINES) >= 0.5)
check("Bandhan parser ignores HDFC line", bp.REGISTRY["bandhan"].detect([HDFC_LINE]) == 0.0)
check("Bandhan parser ignores SBI line", bp.REGISTRY["bandhan"].detect([SBI_LINE]) == 0.0)
check("Bandhan parser ignores Indian Bank line", bp.REGISTRY["bandhan"].detect([IB_LINE]) == 0.0)
check("Bandhan parser ignores Canara lines", bp.REGISTRY["bandhan"].detect(CANARA_LINES) == 0.0)
check("Bandhan is now implemented (not a stub)", bp.REGISTRY["bandhan"].implemented is True)

check("Canara parser detects Canara lines", bp.REGISTRY["canara"].detect(CANARA_LINES) >= 0.5)
check("Canara parser ignores HDFC line", bp.REGISTRY["canara"].detect([HDFC_LINE]) == 0.0)
check("Canara parser ignores SBI line", bp.REGISTRY["canara"].detect([SBI_LINE]) == 0.0)
check("Canara parser ignores Indian Bank line", bp.REGISTRY["canara"].detect([IB_LINE]) == 0.0)
check("Canara parser ignores Bandhan lines", bp.REGISTRY["canara"].detect(BANDHAN_LINES) == 0.0)
check("Canara is now implemented (not a stub)", bp.REGISTRY["canara"].implemented is True)

check("IDBI parser detects IDBI lines", bp.REGISTRY["idbi"].detect(IDBI_LINES) >= 0.5)
check("IDBI beats Indian Bank on IDBI content (both see Cr-balance dates)",
      bp.REGISTRY["idbi"].detect(IDBI_LINES) > bp.REGISTRY["indian_bank"].detect(IDBI_LINES))
check("IDBI parser ignores HDFC line", bp.REGISTRY["idbi"].detect([HDFC_LINE]) == 0.0)
check("IDBI parser ignores SBI line", bp.REGISTRY["idbi"].detect([SBI_LINE]) == 0.0)
check("IDBI parser ignores Indian Bank line", bp.REGISTRY["idbi"].detect([IB_LINE]) == 0.0)
check("IDBI parser ignores Bandhan lines", bp.REGISTRY["idbi"].detect(BANDHAN_LINES) == 0.0)
check("IDBI parser ignores Canara lines", bp.REGISTRY["idbi"].detect(CANARA_LINES) == 0.0)
check("IDBI is now implemented (not a stub)", bp.REGISTRY["idbi"].implemented is True)

# the other parsers must not fire on Bandhan's, Canara's or IDBI's lines
for other in ["hdfc", "sbi", "indian_bank"]:
    check(f"{other} ignores Bandhan lines", bp.REGISTRY[other].detect(BANDHAN_LINES) == 0.0)
    check(f"{other} ignores Canara lines", bp.REGISTRY[other].detect(CANARA_LINES) == 0.0)
for other in ["hdfc", "sbi", "bandhan", "canara"]:
    check(f"{other} ignores IDBI lines", bp.REGISTRY[other].detect(IDBI_LINES) == 0.0)


# --- 3. auto-detect selection ----------------------------------------------
print("\n== auto-detect selection ==")
for lines, expected in [([HDFC_LINE], "hdfc"), ([SBI_LINE], "sbi"), ([IB_LINE], "indian_bank"),
                        (BANDHAN_LINES, "bandhan"), (CANARA_LINES, "canara"), (IDBI_LINES, "idbi")]:
    parser, unrecognized = bp.select_parser("auto", lines)
    check(f"auto -> {expected}", parser.key == expected and not unrecognized)

parser, unrecognized = bp.select_parser("auto", JUNK)
check("auto on unknown -> generic + flagged unrecognized",
      parser.key == "generic" and unrecognized)


# --- 4. explicit bank selection --------------------------------------------
print("\n== explicit bank selection ==")
parser, unrecognized = bp.select_parser("hdfc", [SBI_LINE])   # wrong content, explicit choice
check("explicit hdfc -> hdfc parser, not flagged", parser.key == "hdfc" and not unrecognized)

parser, unrecognized = bp.select_parser("icici", [HDFC_LINE])
check("explicit stub (icici) -> stub parser, flagged unrecognized",
      parser.key == "icici" and unrecognized)
check("icici is a registered but not-implemented stub",
      bp.REGISTRY["icici"].implemented is False)
for stub in ["icici", "union"]:
    check(f"stub '{stub}' registered & not implemented",
          stub in bp.REGISTRY and not bp.REGISTRY[stub].implemented)


# --- 5. password helpers (only if PyMuPDF is available to make an encrypted PDF)
print("\n== password helpers ==")
try:
    import fitz  # PyMuPDF — optional, not a project dependency
except Exception:  # noqa: BLE001
    print("[SKIP] PyMuPDF not installed — cannot synthesise an encrypted PDF")
else:
    tmp = os.path.join(tempfile.gettempdir(), "registry_test_enc.pdf")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "01/04/25 01/04/25 8050.00 119544.23Cr")
    doc.save(tmp, encryption=fitz.PDF_ENCRYPT_AES_128, user_pw="open-sesame", owner_pw="owner-xyz")
    doc.close()
    try:
        check("requires_password True for encrypted PDF", requires_password(tmp) is True)
        check("wrong password rejected", password_is_correct(tmp, "bad") is False)
        check("correct password accepted", password_is_correct(tmp, "open-sesame") is True)
    finally:
        os.remove(tmp)

print("\nALL REGISTRY TESTS PASSED")
