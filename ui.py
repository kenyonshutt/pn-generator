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
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
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


def _load_last_project() -> str:
    try:
        return json.loads(USER_JSON.read_text(encoding="utf-8")).get("last_project", "")
    except Exception:
        return ""


def _save_last_project(name: str) -> None:
    try:
        data = json.loads(USER_JSON.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data["last_project"] = name
    USER_JSON.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_last_sot(project_name: str) -> str:
    try:
        return (
            json.loads(USER_JSON.read_text(encoding="utf-8"))
            .get("last_sot", {})
            .get(project_name, "")
        )
    except Exception:
        return ""


def _save_last_sot(project_name: str, sot: str) -> None:
    try:
        data = json.loads(USER_JSON.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    sots = data.setdefault("last_sot", {})
    sots[project_name] = sot
    USER_JSON.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _text_color_for_accent(hex_color: str) -> str:
    c = QColor(hex_color)
    lum = 0.299 * c.redF() + 0.587 * c.greenF() + 0.114 * c.blueF()
    return "#1a1a1a" if lum > 0.45 else "#ffffff"


def _ensure_project_files(path: str) -> None:
    """Create pn_log.csv and next_up.json if they don't already exist."""
    pnp = Path(path)
    pnp.mkdir(parents=True, exist_ok=True)
    log = pnp / "pn_log.csv"
    if not log.exists():
        log.write_text("pn,who,timestamp,source_of_truth\n", encoding="utf-8")
    nxt = pnp / "next_up.json"
    if not nxt.exists():
        nxt.write_text(json.dumps({"next": 1}, indent=2), encoding="utf-8")


def _validate_project_path(path: str) -> list[str]:
    pnp = Path(path)
    return [] if (pnp / "setup.json").exists() else ["setup.json"]


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
        self._dying_workers: set = (
            set()
        )  # workers orphaned by project switch; kept alive until finished

        self.setWindowTitle("pn-service")
        self.setFixedWidth(420)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self._build_ui()

        last = _load_last_project()
        names = [p["name"] for p in self.projects]
        initial = names.index(last) if last in names else 0
        if initial != 0:
            # Setting the index fires _on_project_changed which calls _select_project.
            self.project_combo.setCurrentIndex(initial)
        else:
            self._select_project(0)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(20, 16, 20, 16)

        # ── Project ────────────────────────────────────────────────────
        proj_row = QHBoxLayout()
        proj_label = QLabel("Project")
        proj_label.setObjectName("fieldLabel")
        proj_row.addWidget(proj_label)
        self.project_combo = QComboBox()
        for p in self.projects:
            self.project_combo.addItem(p["name"])
        self.project_combo.currentIndexChanged.connect(self._on_project_changed)
        proj_row.addWidget(self.project_combo, 1)
        root.addLayout(proj_row)

        # ── Status / retry (hidden when clean) ─────────────────────────
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setVisible(False)
        root.addWidget(self.status_label)

        self.retry_btn = QPushButton("Retry cache refill")
        self.retry_btn.setVisible(False)
        self.retry_btn.clicked.connect(self._on_retry_refill)
        root.addWidget(self.retry_btn)

        # ── Controls grid: source-of-truth + part type ─────────────────
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)

        sot_label = QLabel("Source")
        sot_label.setObjectName("fieldLabel")
        grid.addWidget(sot_label, 0, 0)

        self.sot_group = QButtonGroup(self)
        self.sot_layout = QHBoxLayout()
        self.sot_layout.setSpacing(16)
        self.sot_layout.setContentsMargins(0, 0, 0, 0)
        sot_container = QWidget()
        sot_container.setLayout(self.sot_layout)
        grid.addWidget(sot_container, 0, 1)

        type_label = QLabel("Part type")
        type_label.setObjectName("fieldLabel")
        grid.addWidget(type_label, 1, 0)

        self.type_combo = QComboBox()
        grid.addWidget(self.type_combo, 1, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        # ── Mode toggle: New base PN vs Sub PN ─────────────────────────
        mode_row = QHBoxLayout()
        mode_row.setSpacing(0)
        self._mode_group = QButtonGroup(self)

        self._mode_new_btn = QPushButton("New base PN")
        self._mode_new_btn.setCheckable(True)
        self._mode_new_btn.setChecked(True)
        self._mode_new_btn.setObjectName("modeBtn")
        self._mode_new_btn.clicked.connect(lambda: self._set_mode("new"))

        self._mode_sub_btn = QPushButton("Sub PN")
        self._mode_sub_btn.setCheckable(True)
        self._mode_sub_btn.setObjectName("modeBtn")
        self._mode_sub_btn.clicked.connect(lambda: self._set_mode("sub"))

        self._mode_group.addButton(self._mode_new_btn, 0)
        self._mode_group.addButton(self._mode_sub_btn, 1)
        mode_row.addWidget(self._mode_new_btn, 1)
        mode_row.addWidget(self._mode_sub_btn, 1)
        root.addLayout(mode_row)

        # ── Sub PN input (hidden in New mode) ──────────────────────────
        self._sub_row_widget = QWidget()
        sub_row = QHBoxLayout(self._sub_row_widget)
        sub_row.setContentsMargins(0, 0, 0, 0)
        sub_label = QLabel("Base root")
        sub_label.setObjectName("fieldLabel")
        sub_row.addWidget(sub_label)
        self.base_pn_input = QLineEdit()
        self.base_pn_input.setPlaceholderText("e.g. P-000001")
        sub_row.addWidget(self.base_pn_input, 1)
        self._sub_row_widget.setVisible(False)
        root.addWidget(self._sub_row_widget)

        # ── Primary issue button ────────────────────────────────────────
        self.issue_btn = QPushButton("Issue")
        self.issue_btn.setObjectName("issueBtn")
        self.issue_btn.setMinimumHeight(52)
        self.issue_btn.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.issue_btn.clicked.connect(self._on_issue)
        root.addWidget(self.issue_btn)

        # Keep refs for enable/disable (both modes share one button now)
        self.issue_a_btn = self.issue_btn
        self.issue_b_btn = self.issue_btn

        # ── Result card ────────────────────────────────────────────────
        self._result_card = QWidget()
        self._result_card.setObjectName("resultCard")
        card_layout = QVBoxLayout(self._result_card)
        card_layout.setContentsMargins(16, 12, 16, 12)
        card_layout.setSpacing(6)

        self.result_display = QLabel("—")
        pn_font = QFont()
        pn_font.setPointSize(22)
        pn_font.setFamilies(["Menlo", "Courier New", "Courier"])
        pn_font.setWeight(QFont.Weight.Medium)
        self.result_display.setFont(pn_font)
        self.result_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.result_display.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        card_layout.addWidget(self.result_display)

        copy_btn = QPushButton("Copy")
        copy_btn.setObjectName("copyBtn")
        copy_btn.setMaximumWidth(100)
        copy_btn.clicked.connect(self._copy_result)
        copy_row = QHBoxLayout()
        copy_row.addStretch()
        copy_row.addWidget(copy_btn)
        card_layout.addLayout(copy_row)

        root.addWidget(self._result_card)

        # ── Git log ────────────────────────────────────────────────────
        git_header = QHBoxLayout()
        git_label = QLabel("Git")
        git_label.setObjectName("fieldLabel")
        git_header.addWidget(git_label)
        git_header.addStretch()
        clear_log_btn = QPushButton("clear")
        clear_log_btn.setObjectName("clearBtn")
        clear_log_btn.setMaximumWidth(52)
        clear_log_btn.clicked.connect(self._clear_git_log)
        git_header.addWidget(clear_log_btn)
        root.addLayout(git_header)

        self.git_log = QPlainTextEdit()
        self.git_log.setReadOnly(True)
        self.git_log.setFixedHeight(90)
        self.git_log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        log_font = QFont()
        log_font.setFamilies(["Menlo", "Courier New", "Courier"])
        log_font.setPointSize(10)
        self.git_log.setFont(log_font)
        root.addWidget(self.git_log)

    # ------------------------------------------------------------------
    # Mode toggle (New base PN / Sub PN)
    # ------------------------------------------------------------------

    def _set_mode(self, mode: str) -> None:
        self._sub_row_widget.setVisible(mode == "sub")
        self._mode_new_btn.setChecked(mode == "new")
        self._mode_sub_btn.setChecked(mode == "sub")

    def _current_mode(self) -> str:
        return "sub" if self._mode_sub_btn.isChecked() else "new"

    # ------------------------------------------------------------------
    # Unified issue handler
    # ------------------------------------------------------------------

    def _on_issue(self) -> None:
        if self._current_mode() == "sub":
            self._on_issue_sub()
        else:
            self._on_issue_new()

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
        # If a worker is mid-run, keep it alive in _dying_workers until its
        # thread exits — dropping the ref while running causes an abort trap.
        if self._git_active is not None and self._git_active.isRunning():
            dying = self._git_active
            try:
                dying.finished.disconnect(self._on_git_op_done)
            except Exception:
                pass
            dying.finished.connect(lambda: self._dying_workers.discard(dying))
            self._dying_workers.add(dying)
        self._git_active = None
        self._git_running = False
        self._refill_queued = False

        _ensure_project_files(proj["path"])

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
        if 0 <= index < len(self.projects):
            _save_last_project(self.projects[index]["name"])

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
        try:
            self.sot_group.buttonClicked.disconnect()
        except Exception:
            pass

        project_name = self._current_project["name"] if self._current_project else ""
        last_sot = _load_last_sot(project_name)

        matched = False
        for i, src in enumerate(sources):
            rb = QRadioButton(src)
            self.sot_group.addButton(rb, i)
            self.sot_layout.addWidget(rb)
            if src == last_sot:
                rb.setChecked(True)
                matched = True
        if not matched and self.sot_group.buttons():
            self.sot_group.buttons()[0].setChecked(True)

        self.sot_group.buttonClicked.connect(self._on_sot_changed)

    def _on_sot_changed(self, btn: QRadioButton) -> None:
        if self._current_project:
            _save_last_sot(self._current_project["name"], btn.text())

    # ------------------------------------------------------------------
    # Accent / styling
    # ------------------------------------------------------------------

    def _apply_accent(self, hex_color: str) -> None:
        fg = _text_color_for_accent(hex_color)
        # Derive a slightly darker shade for the card and hover states
        c = QColor(hex_color)
        dark = QColor.fromHsvF(
            c.hsvHueF(),
            min(c.hsvSaturationF() * 1.1, 1.0),
            max(c.valueF() - 0.12, 0.0),
        ).name()
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background-color: {hex_color};
                color: {fg};
                font-size: 13px;
            }}

            /* Field labels */
            QLabel#fieldLabel {{
                font-size: 11px;
                font-weight: 600;
                opacity: 0.75;
            }}

            /* Dropdowns and text inputs */
            QComboBox, QLineEdit {{
                background-color: rgba(0,0,0,0.18);
                color: {fg};
                border: 1px solid rgba(255,255,255,0.25);
                border-radius: 6px;
                padding: 5px 8px;
                min-height: 28px;
            }}
            QComboBox::drop-down {{ border: none; width: 24px; }}
            QComboBox QAbstractItemView {{
                background-color: {dark};
                color: {fg};
                selection-background-color: rgba(255,255,255,0.2);
                border: none;
            }}

            /* Radio buttons */
            QRadioButton {{ color: {fg}; spacing: 6px; }}

            /* Mode toggle buttons */
            QPushButton#modeBtn {{
                background-color: rgba(0,0,0,0.18);
                color: {fg};
                border: 1px solid rgba(255,255,255,0.25);
                border-radius: 0px;
                padding: 6px 0px;
                font-weight: 500;
            }}
            QPushButton#modeBtn:first-of-type {{
                border-radius: 0px;
            }}
            QPushButton#modeBtn:checked {{
                background-color: rgba(255,255,255,0.22);
                font-weight: 700;
            }}
            QPushButton#modeBtn:hover:!checked {{
                background-color: rgba(255,255,255,0.10);
            }}

            /* Primary issue button */
            QPushButton#issueBtn {{
                background-color: {fg};
                color: {hex_color};
                border: none;
                border-radius: 8px;
                font-size: 16px;
                font-weight: 700;
                letter-spacing: 0.5px;
            }}
            QPushButton#issueBtn:hover {{
                background-color: rgba(255,255,255,0.92);
            }}
            QPushButton#issueBtn:pressed {{
                background-color: rgba(255,255,255,0.75);
            }}
            QPushButton#issueBtn:disabled {{
                background-color: rgba(255,255,255,0.25);
                color: rgba(0,0,0,0.35);
            }}

            /* Result card */
            QWidget#resultCard {{
                background-color: {dark};
                border-radius: 10px;
            }}

            /* Copy button inside card */
            QPushButton#copyBtn {{
                background-color: rgba(255,255,255,0.15);
                color: {fg};
                border: 1px solid rgba(255,255,255,0.25);
                border-radius: 5px;
                padding: 3px 12px;
                font-size: 11px;
            }}
            QPushButton#copyBtn:hover {{
                background-color: rgba(255,255,255,0.25);
            }}

            /* Retry button */
            QPushButton#retryBtn, QPushButton {{
                background-color: rgba(0,0,0,0.15);
                color: {fg};
                border: 1px solid rgba(255,255,255,0.3);
                border-radius: 6px;
                padding: 5px 14px;
            }}
            QPushButton:hover {{
                background-color: rgba(255,255,255,0.12);
            }}
            QPushButton:disabled {{
                color: rgba(255,255,255,0.3);
            }}

            /* Small clear button */
            QPushButton#clearBtn {{
                background-color: transparent;
                color: {fg};
                border: 1px solid rgba(255,255,255,0.2);
                border-radius: 4px;
                padding: 2px 6px;
                font-size: 11px;
            }}
            QPushButton#clearBtn:hover {{
                background-color: rgba(255,255,255,0.1);
            }}

            /* Git log */
            QPlainTextEdit {{
                background-color: rgba(0,0,0,0.25);
                color: {fg};
                border: 1px solid rgba(255,255,255,0.15);
                border-radius: 6px;
            }}
        """)

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def _show_error(self, msg: str) -> None:
        self.status_label.setText(f"⚠ {msg}")
        self.status_label.setStyleSheet(
            "color: #ff5555; font-weight: 600; font-size: 12px;"
        )
        self.status_label.setVisible(True)

    def _show_info(self, msg: str) -> None:
        self.status_label.setText(msg)
        self.status_label.setStyleSheet("font-size: 12px; opacity: 0.8;")
        self.status_label.setVisible(True)

    def _clear_status(self) -> None:
        self.status_label.setText("")
        self.status_label.setVisible(False)

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
