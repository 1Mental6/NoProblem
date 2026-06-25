# Bank Statement PDF → Excel Converter

A resilient Python desktop tool that converts unstructured PDF bank statements
from **any Indian bank** (SBI, HDFC, ICICI, Axis, Kotak, PNB, …) into a clean,
well-structured Excel file — regardless of layout, on PDFs that run to hundreds
of pages, and **without mixing data between transactions** when descriptions
wrap across multiple lines.

## Download & Run (no Python needed)

The easy way, for everyday use on Windows:

1. Open the [**Releases**](../../releases) page and download the latest
   `BankStatementConverter-windows.zip`.
2. **Extract** the zip anywhere (your Desktop is fine). Keep the folder intact —
   the program needs the files that sit next to it.
3. Open the extracted **`BankStatementConverter`** folder and **double-click
   `BankStatementConverter.exe`**.

That's it — no Python, no installer. (Windows SmartScreen may warn about an
unrecognised app the first time, since the build is unsigned: click
**More info → Run anyway**.)

Then: **Select PDF(s)** → **Parse / Preview** → **Export to Excel**, and choose
the folder to save the `*_organized.xlsx` files into.

### Your data never leaves your computer

The tool runs **100% locally**. It reads the PDFs you pick and writes Excel files
to the folder you choose — **nothing is uploaded**, sent to a server, or shared.
It works with your internet disconnected.

### ⚠ Always review rows flagged "CHECK"

Every row's running balance is verified (`previous + credit − debit = balance`).
A row that doesn't reconcile is flagged **`CHECK`** and highlighted yellow in both
the preview and the exported Excel. **Check each flagged row against the original
statement** before trusting the figures — it usually points to an unusual layout
or (on scanned PDFs) an OCR misread.

### Scanned / image-only PDFs need OCR (optional, separate install)

The released `.exe` handles normal **text-based** PDF statements out of the box.
It does **not** include OCR, so *scanned* or photographed statements need two free
external programs, installed separately (they are deliberately **not** bundled, to
keep the download lean):

| Program       | Needed for           | Download |
|---------------|----------------------|----------|
| Tesseract-OCR | reading scanned text | https://github.com/UB-Mannheim/tesseract/wiki |
| Poppler       | rendering PDF pages  | https://github.com/oschwartz10612/poppler-windows/releases |

Full OCR also requires running the developer build with the OCR extras enabled —
see *[Build from source](#build-from-source-developers)* below.

## Why it works on layouts it has never seen

- **Coordinate-based column assignment.** Words are extracted with their x/y
  positions (pdfplumber). Columns are located from the header, then every value
  is placed into a column by its **horizontal position** — never by text reading
  order. That's what keeps cheque numbers, debits, credits and balances in the
  right columns even when the PDF's text stream is jumbled.
- **Fuzzy header mapping** (`rapidfuzz`) maps wildly different bank headers onto
  one schema, so unseen column-name variants still map.
- **Strict multi-line reassembly.** A transaction is anchored by a row with a
  date *and* an amount; every following text-only line is appended to *that*
  transaction's Particulars, in original top-to-bottom order. Descriptions can't
  reorder, and fragments can't leak into a neighbour.

## Features

- **PyQt6 GUI** — select one/many PDFs, preview parsed rows (capped at 500 for
  responsiveness; full data still exports), "page X of Y" progress bar, export.
  All extraction runs on a **background thread** so the UI never freezes.
- **Layered extraction** — pdfplumber coordinates first; automatic fallback to
  `camelot` (lattice → stream) when a page looks structurally empty; **OCR**
  (`pytesseract` + `pdf2image`) for scanned/image PDFs (flagged for review).
- **Dual date columns** — many statements show two dates side by side; the left
  maps to **Date**, the right to **Value Date** (resolved by x-order when both
  are literally "Date").
- **Opening balance** — a "Brought Forward" / "Opening Balance" row (balance, no
  debit/credit) is captured as the opening balance, shown in the summary, and
  used to validate the **first** real transaction so it reads OK (not "—").
- **Messy-case cleaning** — single Amount column + Dr/Cr (or ±) → split into
  Debit/Credit; strip commas, `₹`/`Rs.`, trailing `Cr`/`Dr`; Indian grouping
  (`1,00,000`, `1,19,544.23`) → float; auto date-format detection (`DD/MM/YY`,
  `DD-MMM-YYYY`, …) → one consistent format; repeated headers, footers, page
  numbers and disclaimers stripped.
- **Large-PDF safe** — pages streamed and normalized one at a time (flat memory);
  over Excel's ~1,048,576-row limit → split across sheets.
- **Validation** — `previous_balance + credit − debit == current_balance` (±0.01)
  → `OK` / `CHECK` per row; CHECK rows highlighted in the Excel and the preview.
- **Polished Excel** (`openpyxl`) — frozen, bold, filled header; auto-width;
  right-aligned `#,##0.00` money columns; bottom summary (opening balance, total
  debits, total credits, closing balance, transaction count); plus a second
  **"Debug — Raw Extract"** sheet dumping the raw lines with x/y coordinates,
  page-labelled, so you can diagnose alignment on unusual statements.
- **Never crashes silently** — a PDF that fails reports *why* and the batch
  continues with the rest.

## Project structure

| File                | Responsibility                                                  |
|---------------------|----------------------------------------------------------------|
| `extractor.py`      | PDF → positioned rows (words+coords) / camelot / OCR, streamed  |
| `normalizer.py`     | Coordinate column assignment + header mapping + cleaning + merge |
| `validator.py`      | Running-balance checks → OK / CHECK                             |
| `exporter.py`       | Excel writing (formatting, summary, debug sheet, sheet split)   |
| `main.py`           | PyQt6 GUI + background worker thread                            |
| `acceptance_test.py`| Multi-line-heavy statement test (acceptance criteria a–g)       |
| `selftest.py`       | Offline unit tests for parsing/cleaning/export logic            |

## Build from source (developers)

Requires **Python 3.12**. From the project root (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1        # cmd.exe instead: .venv\Scripts\activate.bat
pip install -r requirements.txt
python main.py
```

(If PowerShell blocks the activate script, either use the `cmd.exe` line above or
run `Set-ExecutionPolicy -Scope Process RemoteSigned` first.)

1. **Select PDF(s)** → 2. **Parse / Preview** (watch the progress bar; CHECK rows
are highlighted; switch files with the dropdown) → 3. **Export to Excel**, then
pick the folder to save each `<original_name>_organized.xlsx` into.

### Optional: enable the camelot / OCR fallbacks

The core path uses `pdfplumber` only. To enable the `camelot` table engine and
OCR for scanned PDFs, install the Python extras plus the external binaries:

```powershell
pip install "camelot-py[cv]" pytesseract pdf2image
```

| Tool          | Needed for         | Download |
|---------------|--------------------|----------|
| Tesseract-OCR | OCR (scanned PDFs) | https://github.com/UB-Mannheim/tesseract/wiki |
| Poppler       | `pdf2image` (OCR)  | https://github.com/oschwartz10612/poppler-windows/releases |
| Ghostscript   | `camelot` lattice  | https://ghostscript.com/releases/gsdnld.html |

If they're not on `PATH`, point the tool at them:

```powershell
$env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"
$env:POPPLER_PATH  = "C:\poppler\Library\bin"
$env:OCR_DPI       = "300"
```

To bundle OCR into a packaged `.exe` as well, also remove the OCR packages from
the `excludes` list in `build.spec` (see the comments in that file).

## Package as a standalone Windows .exe (PyInstaller)

Produces a self-contained folder that end users run without installing Python.
Run these from the project root, starting from a clean checkout:

```powershell
# 1. Create and activate a fresh virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1

# 2. Install the app's runtime dependencies
pip install -r requirements.txt

# 3. Install the build tool (kept out of requirements.txt — build-only)
pip install pyinstaller

# 4. Build using the provided spec (onedir, windowed, no console window)
pyinstaller build.spec
```

The build lands in **`dist\BankStatementConverter\`**:

```
dist\BankStatementConverter\
├─ BankStatementConverter.exe   <-- end users double-click THIS
├─ _internal\                   (Qt plugins, Python runtime, libraries)
└─ ...
```

**To distribute:** zip the **entire `dist\BankStatementConverter\` folder** (not
just the `.exe`) and attach the zip to a **GitHub Release**. End users extract it
and run **`BankStatementConverter.exe` from inside the extracted folder** — the
executable will not start if it is moved away from its sibling files.

> Build choices: the spec uses **onedir**, not onefile (onefile launches slower
> and trips antivirus heuristics). UPX is off for the same reason. `camelot` and
> the OCR stack are excluded to keep the download small, and an optional app icon
> can be turned on in `build.spec` (`icon="app.ico"`).

## Tests

```bash
python acceptance_test.py   # the spec's acceptance criteria (a)-(g)
python selftest.py          # unit tests for cleaning / mapping / export
```

`acceptance_test.py` builds a realistic statement with **absolutely-positioned**
text and right-aligned amounts (not a clean table), then verifies: particulars
read in PDF order; no cross-transaction leakage; dates aligned; cheque numbers in
their own column; both date columns captured; opening balance captured and row 1
validates OK; and the summary totals reconcile.

## A note on resilience

The "Debug — Raw Extract" sheet exists precisely because no heuristic is perfect
on every layout: if a column ever looks off, you can compare the raw coordinates
against the final output to see exactly what happened. When confidence is low
(OCR, or a balance chain that doesn't add up) the tool **tells you** (OCR
warning, CHECK flags) rather than silently emitting wrong data — correctness over
speed.
