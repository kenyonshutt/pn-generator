"""
PyQt6 desktop application for issuing sequential part numbers.

Entry point:
  python ui.py

Design:
  - Issuing a PN is instant: local file ops (pop cache, write CSV) run in the main
    thread (<5 ms), the PN is shown and copied to clipboard immediately.
  - Git commit + push for each issue runs in a background GitPushWorker.
  - After every issue, a proactive background RefillWorker tops the cache back up
    to cache_size if it has dropped below that threshold.
  - Git operations are serialized: a RefillWorker waits until all in-flight
    GitPushWorkers have finished before starting, to avoid push conflicts.
  - The issue buttons are disabled ONLY when the cache is completely empty and a
    blocking refill must complete before issuing can proceed.
"""

import csv
import json
import sys
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import generate

SERVICE_DIR = Path(__file__).parent
PROJECTS_CSV = SERVICE_DIR / "projects.csv"
USER_JSON = SERVICE_DIR / "user.json"


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------


class GitPushWorker(QThread):
    """Commit and push a set of already-written files. Runs entirely in background.

    Uses `result` (not `finished`) for business-logic signals so that QThread's
    built-in `finished` signal remains available for lifecycle management.
    QThread.finished fires *after* run() fully exits, making it safe to release
    the last Python reference to the worker without triggering a GC crash.
    """

    log = pyqtSignal(str)  # status lines for the git log panel
    result = pyqtSignal(str)  # error string; "" on success

    def __init__(
        self, part_numbers_path: str, files: list[Path], message: str, parent=None
    ):
        super().__init__(parent)
        self.part_numbers_path = part_numbers_path
        self.files = files
        self.message = message

    def run(self):
        self.log.emit(f"push: {self.message}")
        ok, err = generate.git_commit_and_push(
            self.part_numbers_path, self.files, self.message
        )
        if ok:
            self.log.emit("  ✓ pushed")
        else:
            self.log.emit(f"  ✗ {err}")
        self.result.emit(err if not ok else "")


class RefillWorker(QThread):
    """Runs a full cache refill (pull → reserve → commit → push).

    Same signal naming convention as GitPushWorker — `result` for business logic,
    QThread.finished for lifecycle/queue advancement.
    """

    log = pyqtSignal(str)  # status lines for the git log panel
    result = pyqtSignal(list, str)  # (newly_reserved, error)

    def __init__(self, part_numbers_path: str, who: str, cache_size: int, parent=None):
        super().__init__(parent)
        self.part_numbers_path = part_numbers_path
        self.who = who
        self.cache_size = cache_size

    def run(self):
        self.log.emit(f"refill: topping up cache (target {self.cache_size})")
        reserved, err = generate.refill_cache(
            self.part_numbers_path, self.who, self.cache_size
        )
        if reserved:
            self.log.emit(f"  ✓ reserved {len(reserved)} numbers, pushed")
        elif not err:
            self.log.emit("  ✓ cache already full, nothing to do")
        else:
            self.log.emit(f"  ✗ {err}")
        self.result.emit(reserved, err)


# ---------------------------------------------------------------------------
# Missing-config dialog
# ---------------------------------------------------------------------------


class MissingConfigDialog(QDialog):
    def __init__(self, missing: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Missing configuration")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("The following required files are missing:"))
        for filename, description in missing:
            frame = QFrame()
            frame.setFrameShape(QFrame.Shape.StyledPanel)
            fl = QVBoxLayout(frame)
            fl.addWidget(QLabel(f"<b>{filename}</b>"))
            fl.addWidget(QLabel(description))
            layout.addWidget(frame)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_projects() -> list[dict]:
    projects = []
    try:
        with PROJECTS_CSV.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                path = row.get("path", "").strip()
                accent = row.get("accent", "#444444").strip()
                name = Path(path).parent.name
                projects.append({"name": name, "path": path, "accent": accent})
    except Exception:
        pass
    return projects


def _load_user() -> str:
    try:
        return json.loads(USER_JSON.read_text(encoding="utf-8")).get("who", "")
    except Exception:
        return ""


def _text_color_for_accent(hex_color: str) -> str:
    c = QColor(hex_color)
    lum = 0.299 * c.redF() + 0.587 * c.greenF() + 0.114 * c.blueF()
    return "#1a1a1a" if lum > 0.45 else "#ffffff"


def _validate_project_path(path: str) -> list[str]:
    pnp = Path(path)
    return [
        f
        for f in ("setup.json", "pn_log.csv", "next_up.json")
        if not (pnp / f).exists()
    ]


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self, projects: list[dict], who: str):
        super().__init__()
        self.projects = projects
        self.who = who
        self.setup: dict = {}
        self._current_project: dict | None = None

        # Git serialization — one operation at a time to avoid ref-lock conflicts.
        self._git_queue: list = []  # QThread instances waiting to run
        self._git_active: QThread | None = None  # strong ref to the running worker
        self._git_running: bool = False
        self._refill_worker: RefillWorker | None = None
        self._refill_queued: bool = False

        self.setWindowTitle("pn-service")
        self.setMinimumWidth(480)
        self._build_ui()
        self._select_project(0)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        # Project selector
        proj_row = QHBoxLayout()
        proj_row.addWidget(QLabel("Project:"))
        self.project_combo = QComboBox()
        for p in self.projects:
            self.project_combo.addItem(p["name"])
        self.project_combo.currentIndexChanged.connect(self._on_project_changed)
        proj_row.addWidget(self.project_combo, 1)
        root.addLayout(proj_row)

        # Status label
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status_label)

        # Retry button — only visible after a blocking refill failure
        self.retry_btn = QPushButton("Retry cache refill")
        self.retry_btn.setVisible(False)
        self.retry_btn.clicked.connect(self._on_retry_refill)
        root.addWidget(self.retry_btn)

        # Source-of-truth radio buttons (populated per project)
        self.sot_group = QButtonGroup(self)
        self.sot_layout = QHBoxLayout()
        sot_container = QWidget()
        sot_container.setLayout(self.sot_layout)
        root.addWidget(sot_container)

        # Part type dropdown
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Part type:"))
        self.type_combo = QComboBox()
        type_row.addWidget(self.type_combo, 1)
        root.addLayout(type_row)

        # Tabs
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        tab_a = QWidget()
        lay_a = QVBoxLayout(tab_a)
        lay_a.setSpacing(10)
        self.issue_a_btn = QPushButton("Issue new base PN")
        self.issue_a_btn.setMinimumHeight(40)
        self.issue_a_btn.clicked.connect(self._on_issue_new)
        lay_a.addWidget(self.issue_a_btn)
        self.tabs.addTab(tab_a, "Option A — New base PN")

        tab_b = QWidget()
        lay_b = QVBoxLayout(tab_b)
        lay_b.setSpacing(10)
        base_row = QHBoxLayout()
        base_row.addWidget(QLabel("Base PN root:"))
        self.base_pn_input = QLineEdit()
        self.base_pn_input.setPlaceholderText("e.g. P-000001")
        base_row.addWidget(self.base_pn_input, 1)
        lay_b.addLayout(base_row)
        self.issue_b_btn = QPushButton("Issue sub PN")
        self.issue_b_btn.setMinimumHeight(40)
        self.issue_b_btn.clicked.connect(self._on_issue_sub)
        lay_b.addWidget(self.issue_b_btn)
        self.tabs.addTab(tab_b, "Option B — Sub PN")

        # Result display
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        root.addWidget(QLabel("Last issued PN:"))
        self.result_display = QLabel("—")
        font = QFont()
        font.setPointSize(20)
        font.setFamilies(["Menlo", "Courier New", "Courier"])
        self.result_display.setFont(font)
        self.result_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.result_display.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        root.addWidget(self.result_display)

        copy_btn = QPushButton("Copy to clipboard")
        copy_btn.clicked.connect(self._copy_result)
        root.addWidget(copy_btn)

        # Git log panel
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep2)

        git_header = QHBoxLayout()
        git_header.addWidget(QLabel("Git"))
        clear_log_btn = QPushButton("clear")
        clear_log_btn.setMaximumWidth(60)
        clear_log_btn.clicked.connect(self._clear_git_log)
        git_header.addStretch()
        git_header.addWidget(clear_log_btn)
        root.addLayout(git_header)

        self.git_log = QPlainTextEdit()
        self.git_log.setReadOnly(True)
        self.git_log.setMaximumHeight(120)
        self.git_log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        log_font = QFont()
        log_font.setFamilies(["Menlo", "Courier New", "Courier"])
        log_font.setPointSize(10)
        self.git_log.setFont(log_font)
        root.addWidget(self.git_log)

    # ------------------------------------------------------------------
    # Project selection
    # ------------------------------------------------------------------

    def _select_project(self, index: int) -> None:
        if not self.projects:
            self._show_error("No projects found in projects.csv.")
            return

        proj = self.projects[index]
        self._current_project = proj
        self._apply_accent(proj["accent"])
        self.retry_btn.setVisible(False)
        self._git_queue.clear()
        self._git_active = None
        self._git_running = False
        self._refill_queued = False

        missing = _validate_project_path(proj["path"])
        if missing:
            self._show_error(f"Missing files in project: {', '.join(missing)}")
            self._set_issue_enabled(False)
            return

        self.setup = generate.load_setup(proj["path"])
        if not self.setup:
            self._show_error("Could not load setup.json.")
            self._set_issue_enabled(False)
            return

        self._populate_type_combo(self.setup.get("part_types", []))
        self._populate_sot_radios(self.setup.get("source_of_truth", ["git"]))
        self._clear_status()
        self._set_issue_enabled(True)
        self._check_cache_on_load()

    def _on_project_changed(self, index: int) -> None:
        self._select_project(index)

    # ------------------------------------------------------------------
    # Dynamic widget population
    # ------------------------------------------------------------------

    def _populate_type_combo(self, part_types: list) -> None:
        self.type_combo.clear()
        for letter, label in part_types:
            self.type_combo.addItem(f"{letter} — {label}", userData=letter)

    def _populate_sot_radios(self, sources: list[str]) -> None:
        for btn in self.sot_group.buttons():
            self.sot_group.removeButton(btn)
            self.sot_layout.removeWidget(btn)
            btn.deleteLater()
        for i, src in enumerate(sources):
            rb = QRadioButton(src)
            self.sot_group.addButton(rb, i)
            self.sot_layout.addWidget(rb)
            if i == 0:
                rb.setChecked(True)

    # ------------------------------------------------------------------
    # Accent / styling
    # ------------------------------------------------------------------

    def _apply_accent(self, hex_color: str) -> None:
        fg = _text_color_for_accent(hex_color)
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background-color: {hex_color};
                color: {fg};
            }}
            QPushButton {{
                background-color: {hex_color};
                color: {fg};
                border: 2px solid {fg};
                border-radius: 4px;
                padding: 6px 12px;
            }}
            QPushButton:hover {{
                background-color: {fg};
                color: {hex_color};
            }}
            QPushButton:disabled {{ opacity: 0.4; }}
            QComboBox, QLineEdit {{
                background-color: rgba(255,255,255,0.15);
                color: {fg};
                border: 1px solid {fg};
                border-radius: 3px;
                padding: 4px;
            }}
            QTabWidget::pane {{
                border: 1px solid {fg};
                border-radius: 4px;
            }}
            QTabBar::tab {{
                background-color: rgba(0,0,0,0.15);
                color: {fg};
                padding: 6px 14px;
                border: 1px solid {fg};
                border-bottom: none;
                border-radius: 4px 4px 0 0;
            }}
            QTabBar::tab:selected {{ background-color: {hex_color}; }}
            QLabel, QRadioButton {{ color: {fg}; }}
            QPlainTextEdit {{
                background-color: rgba(0,0,0,0.25);
                color: {fg};
                border: 1px solid rgba(128,128,128,0.4);
                border-radius: 3px;
            }}
        """)

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def _show_error(self, msg: str) -> None:
        self.status_label.setText(f"⚠ {msg}")
        self.status_label.setStyleSheet("color: #ff4444; font-weight: bold;")

    def _show_info(self, msg: str) -> None:
        self.status_label.setText(msg)
        self.status_label.setStyleSheet("")

    def _clear_status(self) -> None:
        self.status_label.setText("")
        self.status_label.setStyleSheet("")

    def _set_issue_enabled(self, enabled: bool) -> None:
        self.issue_a_btn.setEnabled(enabled)
        self.issue_b_btn.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cache_file(self) -> Path | None:
        if not self._current_project:
            return None
        return Path(self._current_project["path"]) / f"{self.who}_cache.json"

    def _cache_remaining(self) -> int:
        cf = self._cache_file()
        return len(generate._read_cache(cf)) if cf else 0

    def _cache_target(self) -> int:
        return self.setup.get("cache_size", 10)

    # ------------------------------------------------------------------
    # Cache load check (startup / project switch)
    # ------------------------------------------------------------------

    def _check_cache_on_load(self) -> None:
        """On startup or project switch: if cache is empty, do a blocking refill."""
        if self._cache_remaining() == 0:
            self._start_blocking_refill()

    # ------------------------------------------------------------------
    # Blocking refill (cache is empty — must succeed before issuing)
    # ------------------------------------------------------------------

    def _start_blocking_refill(self) -> None:
        if not self._current_project:
            return
        self._set_issue_enabled(False)
        self.retry_btn.setVisible(False)
        self._show_info("Refilling cache…")
        worker = RefillWorker(
            self._current_project["path"], self.who, self._cache_target()
        )
        worker.log.connect(self._git_log)
        worker.result.connect(self._on_blocking_refill_done)
        self._refill_worker = worker
        worker.start()

    def _on_blocking_refill_done(self, reserved: list, err: str) -> None:
        if err:
            self._show_error(f"Cache refill failed: {err}")
            self.retry_btn.setVisible(True)
            self._set_issue_enabled(False)
        else:
            self._clear_status()
            self._set_issue_enabled(True)
            self.retry_btn.setVisible(False)
            self._show_info(f"Ready — {len(reserved)} numbers cached.")
        self._refill_worker = None

    def _on_retry_refill(self) -> None:
        self.retry_btn.setVisible(False)
        self._start_blocking_refill()

    # ------------------------------------------------------------------
    # Git operation queue — serializes all pushes/refills one at a time
    # ------------------------------------------------------------------

    def _enqueue_git(self, worker: QThread) -> None:
        """Add a git worker to the queue and start it if nothing is running."""
        self._git_queue.append(worker)
        if not self._git_running:
            self._run_next_git()

    def _run_next_git(self) -> None:
        if not self._git_queue:
            self._git_running = False
            self._git_active = None
            return
        self._git_running = True
        # Keep a strong ref in _git_active so Python doesn't GC the object while
        # the thread is running. We release it only from _on_git_op_done, which is
        # connected to QThread.finished — which fires *after* run() fully exits.
        self._git_active = self._git_queue.pop(0)
        self._git_active.start()

    def _on_git_op_done(self) -> None:
        """Connected to QThread.finished (built-in) — fires after run() fully exits."""
        self._git_active = None
        self._git_running = False
        self._run_next_git()

    # ------------------------------------------------------------------
    # Background (proactive) refill — keeps cache topped up silently
    # ------------------------------------------------------------------

    def _maybe_proactive_refill(self) -> None:
        """
        Called after every issue. If cache is below target and a refill isn't
        already queued, enqueue a background RefillWorker. The queue ensures
        the refill only runs after any preceding push workers have completed.
        """
        if self._refill_queued:
            return
        if self._cache_remaining() >= self._cache_target():
            return
        if not self._current_project:
            return

        self._refill_queued = True
        worker = RefillWorker(
            self._current_project["path"], self.who, self._cache_target()
        )
        worker.log.connect(self._git_log)
        worker.result.connect(self._on_proactive_refill_done)
        worker.finished.connect(
            self._on_git_op_done
        )  # QThread built-in; fires after exit
        self._refill_worker = worker
        self._enqueue_git(worker)

    def _on_proactive_refill_done(self, reserved: list, err: str) -> None:
        self._refill_queued = False
        self._refill_worker = None
        if err:
            if self._cache_remaining() == 0:
                self._show_error(f"Cache refill failed: {err}")
                self.retry_btn.setVisible(True)
                self._set_issue_enabled(False)
            else:
                self._show_info(
                    f"Background refill failed (cache still has numbers): {err}"
                )

    # ------------------------------------------------------------------
    # Git push worker
    # ------------------------------------------------------------------

    def _enqueue_push_worker(self, files: list[Path], message: str) -> None:
        """Create a GitPushWorker and add it to the serialized git queue."""
        worker = GitPushWorker(self._current_project["path"], files, message)
        worker.log.connect(self._git_log)
        worker.result.connect(self._on_push_done)
        worker.finished.connect(
            self._on_git_op_done
        )  # QThread built-in; fires after exit
        self._enqueue_git(worker)

    def _on_push_done(self, err: str) -> None:
        pass  # errors already logged to git panel; push failures leave local commit intact

    # ------------------------------------------------------------------
    # Issue — Option A (new base PN)
    # ------------------------------------------------------------------

    def _on_issue_new(self) -> None:
        if not self._current_project:
            return

        # If cache is empty we must refill first (blocking path).
        if self._cache_remaining() == 0:
            self._start_blocking_refill_then_issue_new()
            return

        self._do_issue_new()

    def _start_blocking_refill_then_issue_new(self) -> None:
        self._set_issue_enabled(False)
        self._show_info("Cache empty — refilling before issuing…")
        worker = RefillWorker(
            self._current_project["path"], self.who, self._cache_target()
        )
        worker.log.connect(self._git_log)
        worker.result.connect(self._on_blocking_refill_then_issue_new)
        self._refill_worker = worker
        worker.start()

    def _on_blocking_refill_then_issue_new(self, reserved: list, err: str) -> None:
        self._refill_worker = None
        if err:
            self._show_error(f"Cache refill failed: {err}")
            self.retry_btn.setVisible(True)
            return
        self._set_issue_enabled(True)
        self._do_issue_new()

    def _do_issue_new(self) -> None:
        pnp = Path(self._current_project["path"])
        type_letter = self.type_combo.currentData() or self.type_combo.currentText()[0]
        sot = self._selected_sot()

        # ---- INSTANT: local ops in main thread ----
        pn, err = generate.issue_new_base_pn_local(
            pnp, self.who, self.setup, type_letter, sot
        )
        if err:
            self._show_error(err)
            return

        self.result_display.setText(pn)
        QApplication.clipboard().setText(pn)
        self._clear_status()

        # ---- BACKGROUND: git commit + push ----
        cache_file = pnp / f"{self.who}_cache.json"
        self._enqueue_push_worker([pnp / "pn_log.csv", cache_file], f"pn: {pn}")

        # ---- BACKGROUND: proactive refill if cache dropped below target ----
        self._maybe_proactive_refill()

    # ------------------------------------------------------------------
    # Issue — Option B (sub PN)
    # ------------------------------------------------------------------

    def _on_issue_sub(self) -> None:
        base_root = self.base_pn_input.text().strip()
        if not base_root:
            self._show_error("Enter a base PN root before issuing a sub PN.")
            return
        if not self._current_project:
            return

        pnp = Path(self._current_project["path"])
        type_letter = self.type_combo.currentData() or self.type_combo.currentText()[0]
        sot = self._selected_sot()

        # ---- INSTANT: local ops ----
        pn, err = generate.issue_sub_pn_local(
            pnp, self.who, self.setup, base_root, type_letter, sot
        )
        if err:
            self._show_error(err)
            return

        self.result_display.setText(pn)
        QApplication.clipboard().setText(pn)
        self._clear_status()

        # ---- BACKGROUND: git commit + push ----
        self._enqueue_push_worker([pnp / "pn_log.csv"], f"pn: {pn}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _selected_sot(self) -> str:
        btn = self.sot_group.checkedButton()
        return btn.text() if btn else "git"

    def _copy_result(self) -> None:
        text = self.result_display.text()
        if text and text != "—":
            QApplication.clipboard().setText(text)
            self._show_info("Copied to clipboard.")

    def _git_log(self, line: str) -> None:
        """Append a line to the git log panel and scroll to bottom."""
        self.git_log.appendPlainText(line)
        self.git_log.verticalScrollBar().setValue(
            self.git_log.verticalScrollBar().maximum()
        )

    def _clear_git_log(self) -> None:
        self.git_log.clear()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("pn-service")

    missing_configs: list[tuple[str, str]] = []
    if not PROJECTS_CSV.exists():
        missing_configs.append(
            (
                "projects.csv",
                "Registry of project repos. Two columns: path, accent.\n"
                "Example:\n  path,accent\n  /Users/you/project/part_numbers,#E8593C",
            )
        )
    if not USER_JSON.exists():
        missing_configs.append(
            (
                "user.json",
                'Local user identity. Example:\n  {"who": "yourname"}',
            )
        )

    if missing_configs:
        MissingConfigDialog(missing_configs).exec()
        if not PROJECTS_CSV.exists() or not USER_JSON.exists():
            sys.exit(1)

    projects = _load_projects()
    who = _load_user()

    if not who:
        QMessageBox.critical(None, "Error", "user.json is missing the 'who' field.")
        sys.exit(1)
    if not projects:
        QMessageBox.critical(None, "Error", "projects.csv is empty or unreadable.")
        sys.exit(1)

    window = MainWindow(projects, who)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
