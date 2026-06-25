# -*- mode: python ; coding: utf-8 -*-
"""
build.spec — PyInstaller spec for the Bank Statement PDF -> Excel converter.

Mode    : --onedir (a folder, NOT onefile). Onefile is slow to start on a heavy
          GUI app and is a frequent source of antivirus false positives, so we
          ship a folder instead.
Build   : pyinstaller build.spec
Output  : dist/BankStatementConverter/      <-- ZIP THIS FOLDER for release
Run     : dist/BankStatementConverter/BankStatementConverter.exe
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# ---------------------------------------------------------------------------
# Collect packages with data files / compiled extensions / dynamically-loaded
# plugins that PyInstaller's static analysis can miss. collect_all() bundles a
# package's code, data AND binaries.
#
# For PyQt6 this is the important one: it pulls in the Qt "platforms" plugins
# (qwindows.dll, etc.). Without them the process starts but no window ever
# appears ("could not find or load the Qt platform plugin 'windows'").
# ---------------------------------------------------------------------------
for _pkg in ("PyQt6", "pdfplumber", "pdfminer", "rapidfuzz"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# Pure-python packages that import their submodules dynamically.
for _pkg in ("pdfminer", "openpyxl", "dateutil"):
    hiddenimports += collect_submodules(_pkg)

# Belt-and-suspenders explicit hidden imports — names PyInstaller commonly
# misses. NOTE: "pdfminer.six" is the PyPI/distribution name; the import name
# is simply "pdfminer" (listed below and collected above), so pdfminer.six IS
# bundled — there is no separate "pdfminer.six" module to import.
hiddenimports += [
    "pdfplumber",
    "pdfminer",            # distribution name on PyPI: pdfminer.six
    "rapidfuzz",
    "dateutil",
    "dateutil.parser",
    "openpyxl",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
]

# ---------------------------------------------------------------------------
# Excludes — keep the build lean.
#
# The core text-extraction path does NOT need camelot (lattice table parsing)
# or the OCR stack (pytesseract / pdf2image / opencv). Those are only used for
# SCANNED, image-only PDFs and are imported lazily inside extractor.py, so
# excluding them is safe for normal text-based bank statements.
#
# To RE-ENABLE scanned-PDF / OCR support:
#   1. pip install pytesseract pdf2image opencv-python-headless "camelot-py[cv]"
#   2. Delete the matching names from the `excludes` list below.
#   3. Install the external Tesseract-OCR and Poppler binaries (see README.md).
#      Those native programs are NOT bundled by this spec, by design.
# ---------------------------------------------------------------------------
excludes = [
    "camelot",       # camelot-py: lattice table extraction (scanned/ruled tables)
    "pytesseract",   # OCR engine wrapper          -> needs external Tesseract
    "pdf2image",     # rasterises PDF pages for OCR -> needs external Poppler
    "cv2",           # opencv, pulled in by camelot[cv]
    "tkinter",       # unused GUI toolkit; trims size
]


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # onedir: binaries live in the COLLECT folder
    name="BankStatementConverter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # UPX compression off: it triggers AV flags
    console=False,                  # windowed app -> no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,                      # icon="app.ico",  # optional: put app.ico next to this spec and use this line
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BankStatementConverter",
)
