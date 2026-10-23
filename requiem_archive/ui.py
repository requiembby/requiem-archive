import json
import re
from urllib.parse import urlsplit
import sys
import threading
import logging
import faulthandler
from uuid import uuid4
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal, QUrl, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QListWidget, QTextEdit,
    QSpinBox, QFileDialog, QProgressBar)

from .core import ArchiveClient, Recovery, Cancelled, normalize


class Worker(QThread):
    message = Signal(str)
    result = Signal(object)
    error = Signal(str)

    def __init__(self, action, parent=None):
        super().__init__(parent)
        self.action = action
        self.cancel = threading.Event()

    def run(self):
        try:
            self.result.emit(self.action(ArchiveClient(self.cancel), self.message.emit))
        except Cancelled:
            self.error.emit("Operation cancelled")
        except Exception as exc:
            logging.exception("Background operation failed")
            self.error.emit(f"Operation failed: {exc}")


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.captures = []
        self.lookup_url = ""
        self.last_output = None
        self.setWindowTitle("requiem archive • 0.1")
        self.resize(920, 760)
        box = QWidget()
        self.setCentralWidget(box)
        layout = QVBoxLayout(box)
        title = QLabel("REQUIEM / ARCHIVE")
        title.setStyleSheet("font-size: 30px; font-weight: 700; color: #c4a5ff; padding: 12px 0")
        layout.addWidget(title)
        layout.addWidget(QLabel("Recover pages from the past · Internet Archive only"))
        row = QHBoxLayout()
        self.url = QLineEdit("https://example.com/")
        self.url.setPlaceholderText("Website or page URL")
        self.find = QPushButton("Find snapshots")
        self.find.clicked.connect(self.lookup)
        row.addWidget(self.url)
        row.addWidget(self.find)
        layout.addLayout(row)
        layout.addWidget(QLabel("Timeline · up to 500 snapshots, one per month · UTC dates"))
        self.timeline = QListWidget()
        layout.addWidget(self.timeline)
        layout.addWidget(QLabel("Recovers pages and assets on the same domain. Scripts are disabled.\nAsset snapshot dates may vary. Complete recovery is not guaranteed."))
        row = QHBoxLayout()
        self.folder = QLineEdit(str(Path.home() / "Documents" / "requiem-recovery"))
        self.browse = QPushButton("Browse…")
        self.browse.clicked.connect(self.choose_folder)
        row.addWidget(self.folder)
        row.addWidget(self.browse)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Maximum files"))
        self.limit = QSpinBox()
        self.limit.setRange(1, 10000)
        self.limit.setValue(100)
        row.addWidget(self.limit)
        self.recover = QPushButton("Recover selected snapshot")
        self.recover.clicked.connect(self.download)
        row.addWidget(self.recover)
        self.stop = QPushButton("Cancel")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.cancel)
        row.addWidget(self.stop)
        layout.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        layout.addWidget(self.bar)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)
        self.open = QPushButton("Open output folder")
        self.open.setEnabled(False)
        self.open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_output))))
        layout.addWidget(self.open)

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if path:
            self.folder.setText(path)

    def start(self, action, done):
        if self.worker is not None and self.worker.isRunning():
            return
        for widget in (self.find, self.recover, self.url, self.folder, self.browse, self.limit, self.timeline):
            widget.setEnabled(False)
        self.stop.setEnabled(True)
        self.bar.setRange(0, 0)
        self.worker = Worker(action, self)
        self.worker.message.connect(self.append_message)
        self.worker.error.connect(self.append_message)
        self.worker.result.connect(done)
        self.worker.finished.connect(self.idle)
        self.worker.start()

    @Slot(str)
    def append_message(self, message):
        self.log.append(message)

    @Slot()
    def idle(self):
        for widget in (self.find, self.recover, self.url, self.folder, self.browse, self.limit, self.timeline):
            widget.setEnabled(True)
        self.stop.setEnabled(False)
        self.bar.setRange(0, 1)
        self.bar.setValue(1)

    def lookup(self):
        try:
            self.lookup_url = normalize(self.url.text())
        except ValueError as exc:
            self.log.append(str(exc))
            return
        self.captures = []
        self.timeline.clear()
        self.log.append("Contacting the archive. If unavailable, try again later.")
        self.start(lambda client, log: client.timeline(self.lookup_url), self.show_captures)

    @Slot(object)
    def show_captures(self, captures):
        self.captures = captures
        for c in captures:
            self.timeline.addItem(f"{datetime.strptime(c.timestamp, '%Y%m%d%H%M%S'):%Y-%m-%d %H:%M:%S} UTC    {c.mimetype}    {c.original}")
        if captures:
            self.timeline.setCurrentRow(len(captures) - 1)
        self.log.append(f"Snapshots: {len(captures)}" if captures else "No snapshots found. Check http/https and www in the URL.")

    def download(self):
        index = self.timeline.currentRow()
        if index < 0 or index >= len(self.captures):
            self.log.append("Find and select a snapshot first.")
            return
        capture = self.captures[index]
        parent = Path(self.folder.text()).expanduser().resolve()
        host = urlsplit(capture.original).hostname or "site"
        safe_host = re.sub(r"[^a-zA-Z0-9.-]", "_", host)[:80]
        path = parent / f"{safe_host}_{capture.timestamp}_{uuid4().hex[:8]}"
        maximum = self.limit.value()
        self.pending_output = path
        self.log.append(f"Output folder: {path}")
        self.start(lambda client, log: Recovery(client, log).run(capture.original, capture.timestamp, path, maximum), self.downloaded)

    @Slot(object)
    def downloaded(self, report):
        self.last_output = self.pending_output
        count = sum(f["status"] == "saved" for f in report["files"])
        states = {"complete": "Complete", "partial": "Partially recovered", "cancelled": "Cancelled"}
        self.log.append(f"{states.get(report['status'], report['status'])}. Saved: {count}. Details: report.json")
        self.open.setEnabled(True)

    def cancel(self):
        if self.worker:
            self.worker.cancel.set()
            self.log.append("Cancelling… Waiting for the current network read (up to 25 seconds).")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.cancel()
            self.log.append("Close the window again after the operation finishes.")
            event.ignore()
        else:
            event.accept()


def create_application():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QWidget { background: #15101f; color: #eee8fa; font-family: 'Segoe UI'; font-size: 13px; }
        QLineEdit, QListWidget, QTextEdit, QSpinBox { background: #21182f; border: 1px solid #49345f; border-radius: 6px; padding: 8px; }
        QPushButton { background: #7041ac; border: none; border-radius: 6px; padding: 10px 16px; }
        QPushButton:hover { background: #8959c7; } QPushButton:disabled { background: #30253f; color: #8c809b; }
        QListWidget::item:selected { background: #603b90; } QProgressBar { border: none; background: #291e3b; height: 6px; }
        QProgressBar::chunk { background: #b783fa; }
    """)
    return app


_fault_log = None


def main():
    global _fault_log
    log_path = Path(__file__).resolve().parent.parent / "requiem-error.log"
    _fault_log = log_path.open("a", encoding="utf-8")
    faulthandler.enable(_fault_log)
    logging.basicConfig(filename=str(log_path), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    logging.info("Application started")
    app = create_application()
    window = Window()
    def report_exception(kind, value, tb):
        logging.error("UI exception", exc_info=(kind, value, tb))
        window.append_message(f"Error: {value}. Details: {log_path}")
    sys.excepthook = report_exception
    window.show()
    sys.exit(app.exec())
