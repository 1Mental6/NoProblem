"""
main.py
=======
PyQt6 desktop GUI for the PDF bank-statement -> Excel converter.

Workflow:
    1. "Select PDF(s)"   -> pick one or many statements.
    2. "Parse / Preview" -> extraction runs on a BACKGROUND THREAD (the GUI never
       freezes), streaming page-by-page with a "page X of Y" progress bar. Parsed
       transactions are previewed (capped at 500 rows for responsiveness); rows
       that fail balance validation are highlighted.
    3. "Export to Excel" -> pick ONE destination folder (defaults to the Desktop),
       then each statement -> <chosen_folder>/<original_pdf_name>_organized.xlsx,
       including the "Debug — Raw Extract" sheet.

Everything is wrapped in try/except with clear dialogs: a bad PDF reports why it
failed and the run continues with the rest. Nothing crashes silently.
"""

from __future__ import annotations

import os
import sys
import traceback
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel, QListWidget,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

import exporter
from pipeline import parse_pdf

PREVIEW_CAP = 500          # max rows shown in the on-screen table
DEBUG_LINE_CAP = 100_000   # max raw-debug lines kept in memory for the debug sheet


# ===========================================================================
# Background worker
# ===========================================================================
class ParseWorker(QThread):
    progress = pyqtSignal(str, int, int)        # filename, current_page, total
    file_started = pyqtSignal(str)
    file_done = pyqtSignal(str, dict)
    file_failed = pyqtSignal(str, str)
    all_done = pyqtSignal()

    def __init__(self, pdf_paths: List[str]) -> None:
        super().__init__()
        self.pdf_paths = pdf_paths
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:  # QThread entry point
        for path in self.pdf_paths:
            if self._cancel:
                break
            self.file_started.emit(path)
            try:
                self.file_done.emit(path, self._parse_one(path))
            except Exception as exc:  # noqa: BLE001 - report and keep going
                self.file_failed.emit(path, f"{exc}\n\n{traceback.format_exc(limit=3)}")
        self.all_done.emit()

    def _parse_one(self, path: str) -> dict:
        def on_progress(current: int, total: int) -> None:
            self.progress.emit(os.path.basename(path), current, total)

        return parse_pdf(
            path,
            progress_callback=on_progress,
            cancel_check=lambda: self._cancel,
            collect_debug=True,
            debug_line_cap=DEBUG_LINE_CAP,
        )


# ===========================================================================
# Main window
# ===========================================================================
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Bank Statement PDF → Excel Converter")
        self.resize(1150, 740)

        self.pdf_paths: List[str] = []
        self.results: Dict[str, dict] = {}
        self.worker: Optional[ParseWorker] = None

        self._build_ui()

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)

        top = QHBoxLayout()
        self.btn_select = QPushButton("Select PDF(s)")
        self.btn_select.clicked.connect(self.select_pdfs)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.clear_files)
        self.btn_parse = QPushButton("Parse / Preview")
        self.btn_parse.clicked.connect(self.start_parsing)
        self.btn_parse.setEnabled(False)
        self.btn_export = QPushButton("Export to Excel")
        self.btn_export.clicked.connect(self.export_all)
        self.btn_export.setEnabled(False)
        for b in (self.btn_select, self.btn_clear, self.btn_parse, self.btn_export):
            top.addWidget(b)
        top.addStretch(1)
        root.addLayout(top)

        root.addWidget(QLabel("Selected PDFs:"))
        self.file_list = QListWidget()
        self.file_list.setMaximumHeight(110)
        root.addWidget(self.file_list)

        sel = QHBoxLayout()
        sel.addWidget(QLabel("Preview file:"))
        self.preview_combo = QComboBox()
        self.preview_combo.currentTextChanged.connect(self._on_preview_changed)
        sel.addWidget(self.preview_combo, 1)
        root.addLayout(sel)

        self.table = QTableWidget()
        self.table.setColumnCount(len(exporter.HEADERS))
        self.table.setHorizontalHeaderLabels(exporter.HEADERS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        root.addWidget(self.table, 1)

        self.status = QLabel("Select one or more PDF bank statements to begin.")
        root.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.setCentralWidget(central)

    # -- file selection ----------------------------------------------------
    def select_pdfs(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select PDF bank statement(s)", "", "PDF files (*.pdf)"
        )
        if not paths:
            return
        for p in paths:
            if p not in self.pdf_paths:
                self.pdf_paths.append(p)
                self.file_list.addItem(p)
        self.btn_parse.setEnabled(bool(self.pdf_paths))
        self.status.setText(f"{len(self.pdf_paths)} PDF(s) selected. Click 'Parse / Preview'.")

    def clear_files(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        self.pdf_paths.clear()
        self.results.clear()
        self.file_list.clear()
        self.preview_combo.clear()
        self.table.setRowCount(0)
        self.btn_parse.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.status.setText("Cleared. Select PDF(s) to begin.")

    # -- parsing -----------------------------------------------------------
    def start_parsing(self) -> None:
        if not self.pdf_paths:
            return
        self.results.clear()
        self.preview_combo.clear()
        self.table.setRowCount(0)
        self._set_busy(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)

        self.worker = ParseWorker(list(self.pdf_paths))
        self.worker.progress.connect(self._on_progress)
        self.worker.file_started.connect(self._on_file_started)
        self.worker.file_done.connect(self._on_file_done)
        self.worker.file_failed.connect(self._on_file_failed)
        self.worker.all_done.connect(self._on_all_done)
        self.worker.start()

    def _on_file_started(self, path: str) -> None:
        self.status.setText(f"Parsing {os.path.basename(path)} …")

    def _on_progress(self, filename: str, current: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(current)
        self.status.setText(f"Parsing {filename} — page {current} of {total}")

    def _on_file_done(self, path: str, result: dict) -> None:
        self.results[path] = result
        self.preview_combo.addItem(path)
        n = len(result["transactions"])
        checks = result["summary"]["checks"]
        ocr = " (OCR used — review!)" if result["ocr_used"] else ""
        self.status.setText(
            f"Parsed {os.path.basename(path)}: {n} transactions, "
            f"{checks} row(s) flagged CHECK{ocr}."
        )
        if self.preview_combo.count() == 1:
            self._render_preview(path)

    def _on_file_failed(self, path: str, error: str) -> None:
        QMessageBox.critical(
            self, "Parse failed",
            f"Could not parse:\n{path}\n\nReason:\n{error}\n\nContinuing with the rest.",
        )

    def _on_all_done(self) -> None:
        self._set_busy(False)
        self.progress.setVisible(False)
        ok = len(self.results)
        total = len(self.pdf_paths)
        self.btn_export.setEnabled(ok > 0)
        any_ocr = any(r["ocr_used"] for r in self.results.values())
        msg = f"Done. {ok}/{total} PDF(s) parsed successfully."
        if any_ocr:
            msg += "  ⚠ OCR was used on some files — please review those figures."
        self.status.setText(msg)

    # -- preview -----------------------------------------------------------
    def _on_preview_changed(self, path: str) -> None:
        if path and path in self.results:
            self._render_preview(path)

    def _render_preview(self, path: str) -> None:
        result = self.results.get(path)
        if not result:
            return
        transactions = result["transactions"]
        shown = transactions[:PREVIEW_CAP]

        self.table.setRowCount(len(shown))
        for r, txn in enumerate(shown):
            is_check = txn.get("validation") == "CHECK"
            values = [
                _fmt_date(txn.get("date")),
                _fmt_date(txn.get("value_date")),
                txn.get("particulars") or "",
                txn.get("ref_no") or "",
                _fmt_money(txn.get("debit")),
                _fmt_money(txn.get("credit")),
                _fmt_money(txn.get("balance")),
                txn.get("validation") or "",
            ]
            for c, val in enumerate(values):
                item = QTableWidgetItem(val)
                if c in (4, 5, 6):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if is_check:
                    item.setBackground(QColor("#FFF2CC"))
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()

        note = ""
        if len(transactions) > PREVIEW_CAP:
            note = (f"  (showing first {PREVIEW_CAP} of {len(transactions)} rows — "
                    f"the full data will be exported)")
        self.status.setText(
            f"Previewing {os.path.basename(path)}: {len(transactions)} transactions{note}"
        )

    # -- export ------------------------------------------------------------
    def export_all(self) -> None:
        if not self.results:
            return

        # Let the user choose ONE destination folder for all exported files.
        # Start at the Desktop (fall back to home), NOT the source PDF folder.
        out_dir = QFileDialog.getExistingDirectory(
            self, "Choose a folder to save the Excel file(s)", _default_export_dir()
        )
        if not out_dir:
            return  # user cancelled — abort cleanly, no partial write

        successes, failures = [], []
        self.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for path, result in self.results.items():
                base = os.path.splitext(os.path.basename(path))[0]
                out = os.path.join(out_dir, f"{base}_organized.xlsx")
                try:
                    exporter.export_to_excel(
                        result["transactions"], out,
                        opening_balance=result["opening_balance"],
                        ocr_used=result["ocr_used"],
                        debug_pages=result.get("debug_pages"),
                    )
                    successes.append((out, len(result["transactions"])))
                except PermissionError:
                    failures.append(
                        (out, "Output file is open in Excel — close it and retry.")
                    )
                except Exception as exc:  # noqa: BLE001
                    failures.append((out, str(exc)))
        finally:
            QApplication.restoreOverrideCursor()
            self.setEnabled(True)
        self._report_export(out_dir, successes, failures)

    def _report_export(
        self, out_dir: str, successes: List[Tuple[str, int]], failures: List
    ) -> None:
        lines = []
        if successes:
            lines.append(f"Saved to: {out_dir}\n")
            lines.append("Exported:")
            lines.extend(
                f"  ✓ {path}  ({n} transaction(s))" for path, n in successes
            )
        if failures:
            lines.append("\nFailed:")
            lines.extend(f"  ✗ {p}\n     {why}" for p, why in failures)
        box = QMessageBox(self)
        box.setWindowTitle("Export complete" if not failures else "Export finished with errors")
        box.setIcon(QMessageBox.Icon.Information if not failures else QMessageBox.Icon.Warning)
        box.setText("\n".join(lines) if lines else "Nothing to export.")
        open_btn = None
        if successes:
            open_btn = box.addButton(
                "Open output folder", QMessageBox.ButtonRole.ActionRole
            )
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if open_btn is not None and box.clickedButton() is open_btn:
            _open_in_explorer(out_dir)
        if successes:
            self.status.setText(f"Exported {len(successes)} file(s) to {out_dir}.")

    # -- helpers -----------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self.btn_select.setEnabled(not busy)
        self.btn_clear.setEnabled(not busy)
        self.btn_parse.setEnabled(not busy and bool(self.pdf_paths))
        if busy:
            self.btn_export.setEnabled(False)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt signature
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(3000)
        event.accept()


def _default_export_dir() -> str:
    """Starting folder for the 'save to' dialog: Desktop, or home if absent."""
    home = os.path.expanduser("~")
    desktop = os.path.join(home, "Desktop")
    return desktop if os.path.isdir(desktop) else home


def _open_in_explorer(folder: str) -> None:
    """Open *folder* in Windows Explorer so the user lands on the new file(s)."""
    try:
        os.startfile(folder)  # Windows-only; opens the folder in Explorer
    except Exception:  # noqa: BLE001 - best-effort convenience, never fatal
        try:
            import subprocess
            subprocess.Popen(["explorer", os.path.normpath(folder)])
        except Exception:  # noqa: BLE001
            pass


def _fmt_date(value) -> str:
    if value is None:
        return ""
    try:
        return value.strftime("%d-%m-%Y")
    except AttributeError:
        return str(value)


def _fmt_money(value) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def run_selftest(pdf_path: str) -> int:
    """Headless parse + acceptance report for one PDF (python main.py --selftest <pdf>)."""
    import logging
    logging.basicConfig(level=logging.WARNING, format="LOG[%(levelname)s] %(message)s")

    if not os.path.isfile(pdf_path):
        print(f"File not found: {pdf_path}")
        return 1

    print(f"Parsing: {pdf_path}")
    result = parse_pdf(pdf_path, collect_debug=False)
    txns = result["transactions"]
    summary = result["summary"]

    print(f"Method         : {result['method']}")
    print(f"Opening balance: {result['opening_balance']}")
    print(f"Closing balance: {result['closing_balance']}")
    print(f"Transactions   : {len(txns)}")
    print(f"Validation     : {summary['ok']} OK, {summary['checks']} CHECK")
    print("-" * 70)

    failures = []

    def expect(label, cond):
        print(f"[{'PASS' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    expect("transactions extracted (not zero)", len(txns) > 0)

    # The 01/04/25 transaction (first after Brought Forward).
    first = next((t for t in txns if t.get("date") and t["date"].strftime("%d/%m/%y") == "01/04/25"), None)
    if first is not None:
        print(f"\n01/04/25 -> Debit={first['debit']} Credit={first['credit']} "
              f"Balance={first['balance']} [{first.get('validation')}]")
        print(f"Particulars: {first['particulars']!r}\n")
        expect("01/04/25 Credit == 8050.00", (first["credit"] or 0) == 8050.00)
        expect("01/04/25 Balance == 119544.23", abs((first["balance"] or 0) - 119544.23) < 0.01)
        expected_part = ("TRANSFER FROM 97157057378 /IMPS/P2A/509201599397/ "
                         "/IMPS/GOOGLEINDIAD /BRANCH : ATM SERVICE BRANCH")
        expect("01/04/25 Particulars in correct order", first["particulars"] == expected_part)
        if first["particulars"] != expected_part:
            print(f"   expected: {expected_part!r}")
    else:
        expect("01/04/25 transaction found", False)

    expect("opening balance 111494.23 captured",
           result["opening_balance"] is not None and abs(result["opening_balance"] - 111494.23) < 0.01)
    expect("Brought Forward not emitted as a transaction",
           not any("brought forward" in (t.get("particulars") or "").lower() for t in txns))
    expect("Carried Forward not emitted as a transaction",
           not any("carried forward" in (t.get("particulars") or "").lower() for t in txns))
    expect("all balances validate OK", summary["checks"] == 0)

    print("-" * 70)
    print(f"VERDICT: extracted {len(txns)} transaction(s); "
          f"{'ALL CHECKS PASSED' if not failures else f'{len(failures)} CHECK(S) FAILED: ' + str(failures)}")
    return 0 if not failures else 1


def main() -> int:
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        if i + 1 >= len(sys.argv):
            print('Usage: python main.py --selftest "<file.pdf>"')
            return 2
        return run_selftest(sys.argv[i + 1])

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
