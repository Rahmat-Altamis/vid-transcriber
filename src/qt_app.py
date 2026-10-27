import importlib
import itertools
import json
import os
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFontDatabase, QIcon, QPainter, QPixmap, QAction
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTextEdit, QVBoxLayout, QWidget
)

import appconfig

appconfig.ensure_config()

import hcc
import groqc
import mdr
import main as pipeline


PERSONAS = {
    "formal": ("The Naturalist", "nature-documentary narrator"),
    "sarcastic": ("The Critic", "dry, argumentative"),
    "humorous_tech": ("The Debugger", "tech-humor voice"),
    "humorous_non_tech": ("The Relative", "family-joke voice"),
}

STAGES = [
    ("queued", "Queued"),
    ("loading", "Loading file"),
    ("extracting_frames", "Extracting frames"),
    ("describing", "Describing"),
    ("drafting_opinions", "Drafting captions"),
    ("reconciling", "Reconciling"),
    ("judging", "Judging"),
    ("regenerating", "Regenerating"),
    ("done", "Done"),
]

STAGE_ALIAS = {
    "described": "describing",
    "judged": "judging",
    "transcribing": "extracting_frames",
}

STATUS_COLORS = {
    "ready": "#636e7b",
    "waiting": "#636e7b",
    "running": "#c69026",
    "done": "#57ab5a",
    "error": "#e5534b",
    "cancelled": "#768390",
}

STATUS_TEXT = {
    "ready": "ready",
    "waiting": "waiting in queue",
    "running": "running",
    "done": "done",
    "error": "failed",
    "cancelled": "cancelled",
}

QSS = """
* { font-size: 13px; }
QMainWindow, QDialog, QWidget#root { background: #22272e; color: #adbac7; }
QWidget { color: #adbac7; }
QLabel { background: transparent; }
QLabel#muted, QLabel[muted="true"] { color: #768390; }
QLabel#title { font-size: 18px; font-weight: 600; }
QFrame#topbar { background: #2d333b; border-bottom: 1px solid #444c56; }
QFrame#card, QFrame#panel { background: #2d333b; border: 1px solid #444c56; border-radius: 6px; }
QFrame#card QLabel, QFrame#panel QLabel { border: none; }
QGroupBox { border: 1px solid #444c56; border-radius: 6px; margin-top: 10px; padding: 12px 10px 8px 10px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #768390; text-transform: uppercase; font-size: 11px; }
QListWidget { background: #1c2128; border: 1px solid #444c56; border-radius: 6px; padding: 4px; outline: 0; }
QListWidget::item { padding: 7px 8px; border-radius: 4px; }
QListWidget::item:selected { background: #316dca; color: #cdd9e5; }
QListWidget::item:hover:!selected { background: #373e47; }
QTextEdit, QLineEdit, QSpinBox, QDoubleSpinBox { background: #1c2128; border: 1px solid #444c56; border-radius: 6px; padding: 5px 8px; selection-background-color: #316dca; }
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #539bf5; }
QPushButton { background: #373e47; border: 1px solid #444c56; border-radius: 6px; padding: 6px 14px; }
QPushButton:hover:!disabled { background: #444c56; }
QPushButton:disabled { color: #636e7b; }
QPushButton#primary { background: #347d39; border-color: rgba(205,217,229,0.1); color: #f0f6fc; font-weight: 600; }
QPushButton#primary:hover:!disabled { background: #46954a; }
QPushButton#primary:disabled { background: #2b3a2f; color: #636e7b; }
QPushButton#small { padding: 3px 10px; font-size: 12px; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #444c56; border-radius: 3px; background: #1c2128; }
QCheckBox::indicator:checked { background: #347d39; border-color: #347d39; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #444c56; border-radius: 5px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QSplitter::handle { background: #373e47; }
QMenu { background: #2d333b; border: 1px solid #444c56; padding: 4px; }
QMenu::item { padding: 5px 22px; border-radius: 4px; }
QMenu::item:selected { background: #316dca; }
QToolTip { background: #2d333b; color: #adbac7; border: 1px solid #444c56; }
"""


def mono_font():
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSize(10)
    return font


def dot_icon(color: str) -> QIcon:
    pixmap = QPixmap(14, 14)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(2, 2, 10, 10)
    painter.end()
    return QIcon(pixmap)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_ids = itertools.count(1)


@dataclass
class Job:
    path: str
    id: int = field(default_factory=lambda: next(_ids))
    styles: list = field(default_factory=list)
    status: str = "ready"
    stage: Optional[str] = None
    description: Optional[str] = None
    judge_scores: Optional[dict] = None
    captions: dict = field(default_factory=dict)
    error: Optional[str] = None
    warning: Optional[str] = None

    @property
    def name(self) -> str:
        return Path(self.path).name


class JobSignals(QObject):
    stage = pyqtSignal(int, str, object)
    finished = pyqtSignal(int, object)
    failed = pyqtSignal(int, str)


class JobRunner(QRunnable):
    def __init__(self, job: Job, signals: JobSignals):
        super().__init__()
        self.job_id = job.id
        self.path = job.path
        self.styles = list(job.styles)
        self.signals = signals

    def run(self):
        def on_stage(stage, **extra):
            self.signals.stage.emit(self.job_id, stage, extra)

        try:
            with tempfile.TemporaryDirectory(prefix="capwire_") as tmp:
                captions = pipeline.captvid(
                    self.path,
                    self.styles,
                    Path(tmp),
                    on_stage=on_stage,
                )
            self.signals.finished.emit(self.job_id, captions)
        except Exception as e:
            traceback.print_exc()
            self.signals.failed.emit(
                self.job_id,
                str(e) or e.__class__.__name__,
            )


class FileList(QListWidget):
    def paintEvent(self, e):
        super().paintEvent(e)
        if self.count() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(QColor("#636e7b"))
            painter.drawText(
                self.viewport().rect().adjusted(16, 0, -16, 0),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                "Drop video files here\nor click “Add videos…”",
            )
            painter.end()


class CaptionCard(QFrame):
    def __init__(self, style: str):
        super().__init__()
        self.setObjectName("card")
        name, tag = PERSONAS.get(
            style,
            (style.replace("_", " ").title(), ""),
        )

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)

        name_label = QLabel(name)
        name_label.setStyleSheet("font-weight: 600; font-size: 14px;")

        tag_label = QLabel(tag)
        tag_label.setProperty("muted", True)
        tag_label.setStyleSheet("font-size: 11px;")

        titles.addWidget(name_label)
        titles.addWidget(tag_label)
        header.addLayout(titles, 1)

        self.badge = QLabel("")
        header.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        self.caption = QLabel("Waiting for output.")
        self.caption.setWordWrap(True)
        self.caption.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.caption.setMinimumHeight(52)
        self.caption.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        layout.addWidget(self.caption, 1)

        footer = QHBoxLayout()
        self.scores = QLabel("")
        self.scores.setFont(mono_font())
        self.scores.setStyleSheet("color: #636e7b;")
        footer.addWidget(self.scores, 1)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setObjectName("small")
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy)
        footer.addWidget(self.copy_btn)
        layout.addLayout(footer)

        self._text = ""

    def _copy(self):
        QApplication.clipboard().setText(self._text)
        self.copy_btn.setText("Copied")
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(1200, lambda: self.copy_btn.setText("Copy"))

    def set_data(self, caption: str, scores: Optional[dict], threshold: float):
        self._text = caption or ""

        if caption:
            self.caption.setText(caption)
            self.scores.setStyleSheet("color: #636e7b;")
        else:
            self.caption.setText("Waiting for output.")
            self.caption.setStyleSheet(
                "color: #636e7b; font-style: italic;"
            )

        self.copy_btn.setEnabled(bool(caption))

        if scores:
            accuracy = scores.get("accuracy", 1.0)
            style_match = scores.get("style_match", 1.0)
            self.scores.setText(
                f"judged: acc {accuracy:.2f} · style {style_match:.2f}"
            )

            passed = min(accuracy, style_match) >= threshold

            if passed:
                self.badge.setText("Pass")
                self.badge.setStyleSheet(
                    "color:#57ab5a; background:rgba(87,171,90,0.15); "
                    "border:1px solid rgba(87,171,90,0.4); border-radius:9px; "
                    "padding:1px 8px; font-size:11px;"
                )
            else:
                self.badge.setText("Regenerated")
                self.badge.setStyleSheet(
                    "color:#c69026; background:rgba(198,144,38,0.15); "
                    "border:1px solid rgba(198,144,38,0.4); border-radius:9px; "
                    "padding:1px 8px; font-size:11px;"
                )

            self.badge.setToolTip(
                "" if passed
                else "Scored below the threshold, so the judge rewrote this caption."
            )
        else:
            self.scores.setText("")
            self.badge.setText("")
            self.badge.setStyleSheet("")


class DetailPanel(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.body = QWidget()
        self.body.setObjectName("root")
        self.setWidget(self.body)

        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        self.file_title = QLabel("")
        self.file_title.setObjectName("title")
        self.file_title.setWordWrap(True)

        self.file_meta = QLabel("")
        self.file_meta.setProperty("muted", True)
        self.file_meta.setWordWrap(True)
        self.file_meta.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        layout.addWidget(self.file_title)
        layout.addWidget(self.file_meta)

        self.warn_label = QLabel("")
        self.warn_label.setWordWrap(True)
        self.warn_label.setStyleSheet("color: #c69026;")
        self.warn_label.hide()
        layout.addWidget(self.warn_label)

        row = QHBoxLayout()
        row.setSpacing(12)

        steps_box = QFrame()
        steps_box.setObjectName("panel")
        steps_layout = QVBoxLayout(steps_box)

        self.steps = QLabel("")
        self.steps.setFont(mono_font())
        self.steps.setTextFormat(Qt.TextFormat.RichText)
        steps_layout.addWidget(self.steps)
        steps_layout.addStretch(1)
        steps_box.setFixedWidth(210)
        row.addWidget(steps_box)

        report_box = QFrame()
        report_box.setObjectName("panel")
        report_layout = QVBoxLayout(report_box)

        report_label = QLabel("Report - ground truth")
        report_label.setProperty("muted", True)
        report_label.setStyleSheet("font-size: 11px;")

        self.report = QTextEdit()
        self.report.setReadOnly(True)
        self.report.setFont(mono_font())
        self.report.setMinimumHeight(150)

        report_layout.addWidget(report_label)
        report_layout.addWidget(self.report, 1)
        row.addWidget(report_box, 1)
        layout.addLayout(row)

        self.grid = QGridLayout()
        self.grid.setSpacing(10)
        layout.addLayout(self.grid)
        layout.addStretch(1)

        self.cards: dict[str, CaptionCard] = {}
        self.show_job(None)

    def _set_cards(self, styles):
        if list(self.cards) == list(styles):
            return

        for card in self.cards.values():
            card.setParent(None)
            card.deleteLater()

        self.cards = {}

        for i, style in enumerate(styles):
            card = CaptionCard(style)
            self.cards[style] = card
            self.grid.addWidget(card, i // 2, i % 2)

        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)

    def show_job(self, job: Optional[Job]):
        if job is None:
            self.file_title.setText("No video selected")
            self.file_meta.setText(
                "Add one or more videos on the left, pick the styles, and press Caption."
            )
            self.steps.setText("")
            self.report.setPlainText("")
            self.warn_label.hide()
            self._set_cards([])
            return

        self.file_title.setText(job.name)

        try:
            size = human_size(Path(job.path).stat().st_size)
        except OSError:
            size = "file missing"

        self.file_meta.setText(
            f"{job.path}  ·  {size}  ·  {STATUS_TEXT[job.status]}"
        )

        self.warn_label.setText(
            f"⚠ {job.warning}" if job.warning else ""
        )
        self.warn_label.setVisible(bool(job.warning))

        index = -1
        if job.stage:
            key = STAGE_ALIAS.get(job.stage, job.stage)
            index = next(
                (i for i, (stage, _) in enumerate(STAGES) if stage == key),
                -1,
            )

        lines = []

        for i, (_, label) in enumerate(STAGES):
            if job.status == "error" and i == index:
                icon, color, weight = "✕", "#e5534b", "bold"
            elif i < index or (i == index and job.stage == "done"):
                icon, color, weight = "✓", "#768390", "normal"
            elif i == index and job.status == "running":
                icon, color, weight = "●", "#adbac7", "bold"
            else:
                icon, color, weight = "○", "#636e7b", "normal"

            lines.append(
                f'<span style="color:{color};font-weight:{weight}">'
                f'{icon}&nbsp;&nbsp;{label}</span>'
            )

        self.steps.setText("<br>".join(lines))

        if job.status == "error":
            self.report.setHtml(
                f'<span style="color:#e5534b;font-weight:bold">'
                f'✕ {esc(job.error or "Run failed")}</span>'
            )
        elif job.description:
            self.report.setPlainText(job.description)
        else:
            self.report.setPlainText(
                "Waiting to run." if job.status == "ready" else "Processing…"
            )

        styles = job.styles or list(pipeline.STYLE_DESCRIPTIONS.keys())
        self._set_cards(styles)
        threshold = pipeline.JUDGE_THRESHOLD

        for style, card in self.cards.items():
            scores = (job.judge_scores or {}).get(style)
            card.set_data(
                job.captions.get(style, ""),
                scores,
                threshold,
            )


class ConfigDialog(QDialog):
    LAYOUT = [
        ("Required", [
            ("required", "HACKCLUB_API_KEY", "secret"),
        ]),
        ("Text & judge models", [
            ("groq", "text_model", "text"),
            ("groq", "judge_fallback_model", "text"),
            ("groq", "judge_threshold", "float"),
        ]),
        ("Hack Club AI", [
            ("hackclub", "base_url", "text"),
            ("hackclub", "vision_model", "text"),
            ("hackclub", "fallback_base_url", "text"),
        ]),
        ("Speech-to-text (Replicate via Hack Club)", [
            ("replicate", "base_url", "text"),
            ("replicate", "asr_model", "text"),
            ("replicate", "max_audio_seconds", "int"),
        ]),
        ("Pipeline tuning", [
            ("pipeline", "min_frames", "int"),
            ("pipeline", "max_frames", "int"),
            ("pipeline", "frame_every_sec", "int"),
            ("pipeline", "max_vision_images", "int"),
            ("pipeline", "max_parallel_videos", "int"),
            ("pipeline", "use_audio", "bool"),
        ]),
        ("Groq (optional - only for groq: models)", [
            ("required", "GROQ_API_KEY", "secret"),
            ("groq", "base_url", "text"),
        ]),
    ]

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configuration - config.json")
        self.resize(580, 680)
        self.cfg = cfg
        self.widgets = {}

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        inner = QWidget()
        inner.setObjectName("root")
        layout = QVBoxLayout(inner)

        for title, fields in self.LAYOUT:
            box = QGroupBox(title)
            form = QFormLayout(box)
            form.setFieldGrowthPolicy(
                QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
            )

            for group, key, kind in fields:
                value = cfg.get(group, {}).get(
                    key,
                    appconfig.DEFAULTS[group][key],
                )
                widget = self._make(kind, value)
                self.widgets[(group, key)] = (kind, widget)

                label = key if group == "required" else f"{group}.{key}"
                field_widget = (
                    widget if kind != "secret"
                    else self._secret_row(widget)
                )
                form.addRow(label, field_widget)

            layout.addWidget(box)

        layout.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        note = QLabel(
            "Saved to config.json next to the app. Models use the form "
            "provider:model-id (e.g. hackclub:anthropic/claude-opus-5.5)."
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        outer.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    @staticmethod
    def _make(kind, val):
        if kind in ("text", "secret"):
            widget = QLineEdit(str(val))
            if kind == "secret":
                widget.setEchoMode(QLineEdit.EchoMode.Password)
            return widget

        if kind == "float":
            widget = QDoubleSpinBox()
            widget.setRange(0.0, 1.0)
            widget.setSingleStep(0.05)
            widget.setDecimals(2)
            widget.setValue(float(val))
            return widget

        if kind == "int":
            widget = QSpinBox()
            widget.setRange(1, 999)
            widget.setValue(int(val))
            return widget

        widget = QCheckBox()
        widget.setChecked(bool(val))
        return widget

    @staticmethod
    def _secret_row(edit: QLineEdit):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)

        button = QPushButton("Show")
        button.setObjectName("small")

        def toggle():
            hidden = edit.echoMode() == QLineEdit.EchoMode.Password
            edit.setEchoMode(
                QLineEdit.EchoMode.Normal
                if hidden
                else QLineEdit.EchoMode.Password
            )
            button.setText("Hide" if hidden else "Show")

        button.clicked.connect(toggle)
        layout.addWidget(button)
        return row

    def values(self) -> dict:
        output = json.loads(json.dumps(self.cfg))

        for (group, key), (kind, widget) in self.widgets.items():
            if kind in ("text", "secret"):
                value = widget.text().strip()
            elif kind in ("float", "int"):
                value = widget.value()
            else:
                value = widget.isChecked()

            output.setdefault(group, {})[key] = value

        return output


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TRANSCRIBERVID")
        self.resize(1180, 760)
        self.setAcceptDrops(True)

        self.jobs: dict[int, Job] = {}
        self.items: dict[int, QListWidgetItem] = {}
        self.pool = QThreadPool(self)
        self.signals = JobSignals()
        self.signals.stage.connect(self.on_stage)
        self.signals.finished.connect(self.on_finished)
        self.signals.failed.connect(self.on_failed)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top = QFrame()
        top.setObjectName("topbar")
        header = QHBoxLayout(top)
        header.setContentsMargins(18, 12, 18, 12)

        title_layout = QVBoxLayout()
        title_layout.setSpacing(0)

        title = QLabel("VID transcribe")
        title.setObjectName("title")

        subtitle = QLabel("Multi model video captioning · local files")
        subtitle.setProperty("muted", True)
        subtitle.setStyleSheet("font-size: 11px;")

        title_layout.addWidget(title)
        title_layout.addWidget(subtitle)
        header.addLayout(title_layout, 1)

        self.status_label = QLabel("")
        self.status_label.setFont(mono_font())
        self.status_label.setProperty("muted", True)
        header.addWidget(self.status_label)

        self.config_btn = QPushButton("Config")
        self.config_btn.setObjectName("small")
        self.config_btn.clicked.connect(self.open_config)
        header.addWidget(self.config_btn)

        outer.addWidget(top)

        split = QSplitter()
        outer.addWidget(split, 1)

        left = QWidget()
        left.setObjectName("root")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 14, 8, 14)

        self.list = FileList()
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.list.setIconSize(self.list.iconSize() * 1)
        self.list.currentItemChanged.connect(
            lambda *_: self.refresh_detail()
        )
        self.list.itemSelectionChanged.connect(self.update_buttons)
        self.list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.list.customContextMenuRequested.connect(self.list_menu)
        left_layout.addWidget(self.list, 1)

        row = QHBoxLayout()

        self.add_btn = QPushButton("Add videos…")
        self.add_btn.clicked.connect(self.pick_files)

        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self.remove_selected)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_all)

        row.addWidget(self.add_btn, 1)
        row.addWidget(self.remove_btn)
        row.addWidget(self.clear_btn)
        left_layout.addLayout(row)

        box = QGroupBox("Caption styles")
        box_layout = QVBoxLayout(box)
        self.style_checks: dict[str, QCheckBox] = {}

        for key in pipeline.STYLE_DESCRIPTIONS:
            name, tag = PERSONAS.get(
                key,
                (key.replace("_", " ").title(), ""),
            )
            checkbox = QCheckBox(
                f"{name}  -  {tag}" if tag else name
            )
            checkbox.setChecked(True)
            checkbox.toggled.connect(self.update_buttons)
            self.style_checks[key] = checkbox
            box_layout.addWidget(checkbox)

        left_layout.addWidget(box)

        self.run_btn = QPushButton("Caption videos")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self.run_all)

        self.cancel_btn = QPushButton("Cancel waiting")
        self.cancel_btn.clicked.connect(self.cancel_waiting)

        self.export_btn = QPushButton("Export results…")
        self.export_btn.clicked.connect(self.export_results)

        left_layout.addWidget(self.run_btn)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self.cancel_btn)
        buttons_row.addWidget(self.export_btn)
        left_layout.addLayout(buttons_row)

        left.setMinimumWidth(360)
        split.addWidget(left)

        self.detail = DetailPanel()
        split.addWidget(self.detail)

        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([380, 820])

        self.update_buttons()
        self.update_status()

    def pick_files(self):
        extensions = " ".join(
            f"*{extension}" for extension in pipeline.VIDEO_EXTENSIONS
        )
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Choose video files",
            str(Path.home()),
            f"Video files ({extensions});;All files (*)",
        )

        if paths:
            self.add_paths(paths)

    def add_paths(self, paths, strict=False) -> int:
        extensions = pipeline.VIDEO_EXTENSIONS
        found = []

        for path in paths:
            path = str(path)

            if os.path.isdir(path):
                for directory, _, filenames in os.walk(path):
                    found += [
                        os.path.join(directory, filename)
                        for filename in sorted(filenames)
                        if filename.lower().endswith(extensions)
                    ]
            elif not strict or path.lower().endswith(extensions):
                found.append(path)

        existing = {job.path for job in self.jobs.values()}
        added = 0
        first_item = None

        for path in found:
            if path in existing or not os.path.isfile(path):
                continue

            existing.add(path)
            job = Job(path=path)
            self.jobs[job.id] = job

            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, job.id)
            self.list.addItem(item)
            self.items[job.id] = item

            self.refresh_item(job)
            first_item = first_item or item
            added += 1

        if first_item is not None:
            self.list.setCurrentItem(first_item)

        if added:
            self.statusBar().showMessage(
                f"Added {added} video{'s' if added != 1 else ''}",
                4000,
            )
        elif paths:
            self.statusBar().showMessage(
                "Nothing new to add (duplicates or non-video files)",
                4000,
            )

        self.update_buttons()
        self.update_status()
        return added

    def busy_ids(self):
        return {
            job.id
            for job in self.jobs.values()
            if job.status in ("running", "waiting")
        }

    def remove_selected(self):
        busy = self.busy_ids()

        for item in self.list.selectedItems():
            job_id = item.data(Qt.ItemDataRole.UserRole)

            if job_id in busy:
                continue

            self.list.takeItem(self.list.row(item))
            self.items.pop(job_id, None)
            self.jobs.pop(job_id, None)

        self.refresh_detail()
        self.update_buttons()
        self.update_status()

    def clear_all(self):
        if self.busy_ids():
            QMessageBox.information(
                self,
                "Busy",
                "Wait for running videos to finish (or cancel the waiting ones) first.",
            )
            return

        self.list.clear()
        self.items.clear()
        self.jobs.clear()

        self.refresh_detail()
        self.update_buttons()
        self.update_status()

    def list_menu(self, pos):
        item = self.list.itemAt(pos)

        if not item:
            return

        job = self.jobs[item.data(Qt.ItemDataRole.UserRole)]
        menu = QMenu(self)

        rerun = menu.addAction("Re-run")
        rerun.setEnabled(
            job.status in ("done", "error", "cancelled")
        )

        open_file = menu.addAction("Open file")
        folder = menu.addAction("Show folder")
        remove = menu.addAction("Remove")
        remove.setEnabled(job.id not in self.busy_ids())

        action = menu.exec(self.list.mapToGlobal(pos))

        if action == rerun:
            self.start_jobs([job])
        elif action == open_file:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(job.path)
            )
        elif action == folder:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(Path(job.path).parent))
            )
        elif action == remove:
            self.list.setCurrentItem(item)
            self.remove_selected()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]

        if paths:
            self.add_paths(paths, strict=True)
            event.acceptProposedAction()

    def selected_styles(self):
        return [
            key
            for key, checkbox in self.style_checks.items()
            if checkbox.isChecked()
        ]

    def run_all(self):
        jobs = [
            job
            for job in self.jobs.values()
            if job.status in ("ready", "error", "cancelled")
        ]
        self.start_jobs(jobs)

    def start_jobs(self, jobs):
        styles = self.selected_styles()

        if not styles:
            QMessageBox.warning(
                self,
                "No styles",
                "Tick at least one caption style.",
            )
            return

        try:
            self.pool.setMaxThreadCount(
                max(
                    1,
                    int(
                        pipeline.config["pipeline"][
                            "max_parallel_videos"
                        ]
                    ),
                )
            )
        except (KeyError, ValueError, TypeError):
            self.pool.setMaxThreadCount(4)

        for job in jobs:
            job.styles = list(styles)
            job.status = "waiting"
            job.stage = "queued"
            job.error = None
            job.description = None
            job.judge_scores = None
            job.warning = None
            job.captions = {}

            self.refresh_item(job)
            self.pool.start(JobRunner(job, self.signals))

        self.refresh_detail()
        self.update_buttons()
        self.update_status()

    def cancel_waiting(self):
        self.pool.clear()

        for job in self.jobs.values():
            if job.status == "waiting":
                job.status = "cancelled"
                job.stage = None
                self.refresh_item(job)

        self.refresh_detail()
        self.update_buttons()
        self.update_status()

    def on_stage(self, job_id, stage, extra):
        job = self.jobs.get(job_id)

        if not job:
            return

        if job.status == "waiting":
            job.status = "running"

        if stage == "warning":
            job.warning = extra.get("warning")
            self.after_update(job)
            return

        job.stage = stage

        if "description" in extra:
            job.description = extra["description"]

        if "judge_scores" in extra:
            job.judge_scores = extra["judge_scores"]

        self.after_update(job)

    def on_finished(self, job_id, captions):
        job = self.jobs.get(job_id)

        if not job:
            return

        job.status = "done"
        job.stage = "done"
        job.captions = dict(captions)
        self.after_update(job)

    def on_failed(self, job_id, message):
        job = self.jobs.get(job_id)

        if not job:
            return

        job.status = "error"
        job.error = message
        self.after_update(job)

    def after_update(self, job):
        self.refresh_item(job)

        current = self.list.currentItem()

        if (
            current
            and current.data(Qt.ItemDataRole.UserRole) == job.id
        ):
            self.refresh_detail()

        self.update_buttons()
        self.update_status()

    def refresh_item(self, job: Job):
        item = self.items.get(job.id)

        if not item:
            return

        label = STATUS_TEXT[job.status]

        if job.status == "running" and job.stage:
            key = STAGE_ALIAS.get(job.stage, job.stage)
            label = next(
                (
                    stage_label
                    for stage_key, stage_label in STAGES
                    if stage_key == key
                ),
                label,
            ).lower() + "…"

        item.setText(f"{job.name}\n{label}")
        item.setIcon(dot_icon(STATUS_COLORS[job.status]))
        item.setToolTip(job.path)

    def refresh_detail(self):
        item = self.list.currentItem()
        job = (
            self.jobs.get(item.data(Qt.ItemDataRole.UserRole))
            if item
            else None
        )
        self.detail.show_job(job)

    def update_buttons(self):
        statuses = [
            job.status for job in self.jobs.values()
        ]

        runnable = any(
            status in ("ready", "error", "cancelled")
            for status in statuses
        )

        self.run_btn.setEnabled(
            runnable and bool(self.selected_styles())
        )

        count = sum(
            status in ("ready", "error", "cancelled")
            for status in statuses
        )

        self.run_btn.setText(
            f"Caption {count} video{'s' if count != 1 else ''}"
            if count
            else "Caption videos"
        )

        self.cancel_btn.setEnabled("waiting" in statuses)
        self.export_btn.setEnabled("done" in statuses)

        busy = bool(self.busy_ids())
        self.config_btn.setEnabled(not busy)
        self.clear_btn.setEnabled(bool(self.jobs) and not busy)

        selected_busy = any(
            item.data(Qt.ItemDataRole.UserRole) in self.busy_ids()
            for item in self.list.selectedItems()
        )

        self.remove_btn.setEnabled(
            bool(self.list.selectedItems()) and not selected_busy
        )

    def update_status(self):
        jobs = list(self.jobs.values())

        if not jobs:
            self.status_label.setText("no videos")
            return

        done = sum(job.status == "done" for job in jobs)
        errors = sum(job.status == "error" for job in jobs)
        running = sum(
            job.status in ("running", "waiting")
            for job in jobs
        )

        parts = [f"{done}/{len(jobs)} done"]

        if running:
            parts.insert(0, f"running {running}")

        if errors:
            parts.append(f"{errors} failed")

        self.status_label.setText(" · ".join(parts))

    def export_results(self):
        done = [
            job for job in self.jobs.values()
            if job.status == "done"
        ]

        if not done:
            return

        default = str(
            Path(
                pipeline.config.get(
                    "paths", {}
                ).get("output_path", "results.json")
            ).name
        )

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export results",
            str(Path.home() / default),
            "JSON (*.json)",
        )

        if not path:
            return

        output = [
            {
                "task_id": Path(job.path).stem,
                "video_path": job.path,
                "description": job.description,
                "captions": job.captions,
                "judge_scores": job.judge_scores,
            }
            for job in done
        ]

        Path(path).write_text(
            json.dumps(output, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        self.statusBar().showMessage(
            f"Saved {len(output)} result(s) to {path}",
            5000,
        )

    def open_config(self):
        dialog = ConfigDialog(
            appconfig.load_config(),
            self,
        )

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            appconfig.save_config(dialog.values())

            for module in (hcc, groqc, mdr, pipeline):
                importlib.reload(module)

            self.statusBar().showMessage(
                "Config saved and applied",
                4000,
            )
            self.refresh_detail()

        except Exception as e:
            QMessageBox.critical(
                self,
                "Could not save config",
                str(e),
            )

    def closeEvent(self, event):
        if self.busy_ids():
            answer = QMessageBox.question(
                self,
                "Videos still running",
                "Some videos are still being captioned. Quit anyway?",
            )

            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

            self.pool.clear()
            event.accept()
            os._exit(0)

        event.accept()


def run():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    app.setApplicationName("Video Captioner")
    app.setDesktopFileName("videocaptioner")

    icon_file = appconfig.resource_path("icon.png")
    if icon_file.exists():
        app.setWindowIcon(QIcon(str(icon_file)))

    window = MainWindow()
    window.show()

    if len(sys.argv) > 1:
        window.add_paths(sys.argv[1:])

    sys.exit(app.exec())


if __name__ == "__main__":
    run()