import pdfplumber, sys
pdf = pdfplumber.open(sys.argv[1])
pg = pdf.pages[0]
lines = (pg.extract_text() or "").splitlines()
for i, ln in enumerate(lines):
    if 10 <= i <= 30:
        print(f"{i:3} | {ln}")