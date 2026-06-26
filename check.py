import pdfplumber, sys
pdf = pdfplumber.open(sys.argv[1])
print(f"Total pages: {len(pdf.pages)}")
# Look at page 2 (index 1) where the transactions are
pg = pdf.pages[1] if len(pdf.pages) > 1 else pdf.pages[0]
print("PAGE 2 — CHARS:", len(pg.extract_text() or ""), " WORDS:", len(pg.extract_words()))
print("="*70)
lines = (pg.extract_text() or "").splitlines()
for i, ln in enumerate(lines):
    if i <= 30:
        print(f"{i:3} | {ln}")