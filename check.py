import pdfplumber, sys
pdf = pdfplumber.open(sys.argv[1])
pg = pdf.pages[0]
print("CHARS:", len(pg.extract_text() or ""), " WORDS:", len(pg.extract_words()))
print("="*70)
lines = (pg.extract_text() or "").splitlines()
for i, ln in enumerate(lines):
    print(f"{i:3} | {ln}")