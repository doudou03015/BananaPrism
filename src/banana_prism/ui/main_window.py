"""Chinese-first BananaPrism desktop workspace.

Integration boundaries are deliberately isolated in adapter methods:
``_active_preset``, ``_resolve_api_key``, ``_dispatch_generation``,
``_dispatch_edit`` and ``_store_result``.  The concrete services may therefore
evolve without putting credentials in UI models or coupling queue state to a
particular worker implementation.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from PySide6.QtCore import (
    QByteArray,
    QBuffer,
    QIODevice,
    QPoint,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QImage,
    QImageIOHandler,
    QImageReader,
    QImageWriter,
    QKeySequence,
    QPainter,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from banana_prism.constants import (
    API_PROVIDERS,
    DEFAULT_BRUSH_RADIUS,
    DEFAULT_RATIO,
    EDIT_OUTPUT_DPI,
    MODELS,
    RATIOS,
    SIZES,
    T2I_DPI_BY_SIZE,
)
from banana_prism import __version__
from banana_prism.i18n import tr, user_error_text
from banana_prism.models import (
    ApiPreset,
    EditRequest,
    EditResult,
    GenerationRequest,
    GenerationResult,
    QueueStatus,
    QueuedTask,
    TokenUsage,
)
from banana_prism.services.log_service import redact_for_display
from banana_prism.ui.dialogs.console_dialog import ConsoleDialog
from banana_prism.ui.dialogs.password_dialog import PasswordDialog
from banana_prism.ui.dialogs.preflight_dialog import PreflightDialog
from banana_prism.ui.widgets.image_preview import ImagePreview
from banana_prism.ui.widgets.log_panel import LogPanel
from banana_prism.ui.widgets.text_result_panel import TextResultPanel
from banana_prism.utils.helpers import detect_image_format


@dataclass(frozen=True, slots=True)
class _PresetSnapshot:
    preset_id: str
    name: str
    provider: str
    credential_ref: str
    api_url: str


@dataclass(slots=True)
class _JobContext:
    kind: Literal["generation", "edit"]
    request: GenerationRequest | EditRequest
    preset: _PresetSnapshot
    queue_task: QueuedTask | None = None
    job_id: str | None = None
    cancel_requested: bool = False


class MainWindow(QMainWindow):
    """Main UI controller.

    Concrete ``ImageService`` is expected to expose ``start_generation(**kwargs)``
    / ``start_edit(**kwargs)``, ``cancel(job_id=None)``, ``busy`` and Qt signals
    ``success(job_id, result)``, ``error``, ``text_only``, ``cancelled`` and
    ``progress``.  Callback-style legacy services and simple test fakes are also
    accepted by the dispatch adapters.
    """

    queueChanged = Signal()
    jobActiveChanged = Signal(bool)

    def __init__(
        self,
        settings_service: Any = None,
        image_service: Any = None,
        storage_service: Any = None,
        log_service: Any = None,
        auth_service: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("mainWindow")
        self.setWindowTitle(
            tr("app.window_title", app_name=tr("app.display_name"), version=__version__)
        )
        self.resize(1510, 920)
        self.setMinimumSize(1080, 700)

        self._settings_service = settings_service
        self._image_service = image_service
        self._storage_service = storage_service
        self._log_service = log_service
        self._auth_service = auth_service
        self._ui_mode: Literal["txt2img", "edit"] = "txt2img"
        self._work_bytes: bytes | None = None
        self._work_fmt = "png"
        self._work_source_dpi: tuple[float, float] | None = None
        self._last_result: GenerationResult | EditResult | None = None
        self._work_saved_path: str | None = None
        self._current_job: _JobContext | None = None
        self._queue: list[QueuedTask] = []
        self._queue_presets: dict[int, _PresetSnapshot] = {}
        self._queue_running = False
        self._queue_running_idx = -1
        self._queue_consecutive_failures = 0
        self._close_when_idle = False
        self._close_confirmed = False
        self._console_runtime_presets: tuple[ApiPreset, ...] | None = None

        self._build_actions()
        self._build_ui()
        self._apply_theme()
        self._bind_service_signals()
        self._load_preferences()
        self._refresh_presets()
        self._set_ui_mode("txt2img")
        self._refresh_queue()
        self._refresh_busy_ui()
        self._update_daily_count()
        self._log("info", tr("main.ready"))

    # ------------------------------------------------------------------ UI build
    def _build_actions(self) -> None:
        self.import_action = QAction(tr("main.action.import"), self)
        self.import_action.setShortcut(QKeySequence.StandardKey.Open)
        self.import_action.triggered.connect(self.import_image)
        self.console_action = QAction(tr("main.action.console"), self)
        self.console_action.setShortcut(QKeySequence("Ctrl+,"))
        self.console_action.triggered.connect(self._open_console)
        self.undo_action = QAction(tr("main.action.undo_annotation"), self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(lambda: self._preview.undo())
        # Window-scoped shortcuts preserve the legacy menu-free workspace.
        self.addAction(self.import_action)
        self.addAction(self.console_action)
        self.addAction(self.undo_action)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(14, 10, 14, 14)
        root_layout.setSpacing(10)
        root_layout.addWidget(self._build_header())

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setObjectName("mainSplitter")
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.addWidget(self._build_left_panel())
        self._preview = ImagePreview()
        self._preview.setMinimumWidth(420)
        self._preview.selectionChanged.connect(self._on_selection_changed)
        self._main_splitter.addWidget(self._preview)
        self._main_splitter.addWidget(self._build_right_panel())
        self._main_splitter.setStretchFactor(0, 0)
        self._main_splitter.setStretchFactor(1, 1)
        self._main_splitter.setStretchFactor(2, 0)
        self._main_splitter.setSizes([410, 740, 330])
        root_layout.addWidget(self._main_splitter, 1)
        self.setCentralWidget(root)

        status = QStatusBar()
        self.setStatusBar(status)
        self._daily_label = QLabel(tr("main.daily_count", count=0))
        self._job_status_label = QLabel(tr("main.status.idle"))
        status.addWidget(self._job_status_label, 1)
        status.addPermanentWidget(self._daily_label)

    def _build_header(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("appHeader")
        brand = QLabel(tr("app.brand_mark"))
        brand.setObjectName("brandMark")
        title = QLabel(tr("app.brand_name"))
        title.setObjectName("brandTitle")
        subtitle = QLabel(tr("app.subtitle"))
        subtitle.setObjectName("brandSubtitle")
        title_column = QVBoxLayout()
        title_column.setSpacing(0)
        title_column.addWidget(title)
        title_column.addWidget(subtitle)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.addWidget(brand)
        layout.addLayout(title_column)
        layout.addStretch(1)
        return frame

    def _build_left_panel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setObjectName("controlScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumWidth(350)
        scroll.setMaximumWidth(470)
        body = QWidget()
        body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(10)
        layout.addWidget(self._build_sidebar_actions())
        layout.addWidget(self._build_mode_card())
        layout.addWidget(self._build_parameters_card())
        layout.addWidget(self._build_request_card())
        layout.addWidget(self._build_queue_card())
        layout.addStretch(1)
        scroll.setWidget(body)
        return scroll

    def _build_sidebar_actions(self) -> QWidget:
        frame, layout = self._card(tr("main.card.workspace"))
        import_button = QPushButton(tr("main.button.import_image"))
        import_button.setObjectName("sidebarImportButton")
        import_button.clicked.connect(self.import_image)
        console_button = QPushButton(tr("main.action.console"))
        console_button.setObjectName("consoleButton")
        console_button.clicked.connect(self._open_console)
        row = QHBoxLayout()
        row.addWidget(import_button)
        row.addWidget(console_button)
        layout.addLayout(row)
        self._open_saved_btn = QPushButton(tr("main.button.open_save_directory"))
        self._open_saved_btn.setObjectName("openSavedDirectoryButton")
        self._open_saved_btn.setEnabled(False)
        self._open_saved_btn.clicked.connect(self._open_saved_directory)
        self._copy_saved_btn = QPushButton(tr("main.button.copy_save_path"))
        self._copy_saved_btn.setObjectName("copySavedPathButton")
        self._copy_saved_btn.setEnabled(False)
        self._copy_saved_btn.clicked.connect(self._copy_saved_path)
        saved_row = QHBoxLayout()
        saved_row.addWidget(self._open_saved_btn)
        saved_row.addWidget(self._copy_saved_btn)
        layout.addLayout(saved_row)
        return frame

    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setProperty("card", True)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(13, 12, 13, 13)
        layout.setSpacing(9)
        heading = QLabel(title)
        heading.setProperty("cardTitle", True)
        layout.addWidget(heading)
        return frame, layout

    def _build_mode_card(self) -> QWidget:
        frame, layout = self._card(tr("main.card.mode"))
        self._t2i_mode_btn = QPushButton(tr("main.mode.generate"))
        self._t2i_mode_btn.setObjectName("generationModeButton")
        self._edit_mode_btn = QPushButton(tr("main.mode.edit"))
        self._edit_mode_btn.setObjectName("editModeButton")
        for button in (self._t2i_mode_btn, self._edit_mode_btn):
            button.setCheckable(True)
            button.setProperty("segment", True)
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self._t2i_mode_btn)
        group.addButton(self._edit_mode_btn)
        self._t2i_mode_btn.clicked.connect(lambda: self._set_ui_mode("txt2img"))
        self._edit_mode_btn.clicked.connect(lambda: self._set_ui_mode("edit"))
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(self._t2i_mode_btn)
        row.addWidget(self._edit_mode_btn)
        layout.addLayout(row)
        return frame

    def _build_parameters_card(self) -> QWidget:
        frame, layout = self._card(tr("main.card.parameters"))
        self._preset_combo = QComboBox()
        self._preset_combo.setObjectName("presetCombo")
        self._preset_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._preset_combo.setMinimumContentsLength(18)
        self._preset_combo.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        self._preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self._model_combo = QComboBox()
        self._model_combo.setObjectName("modelCombo")
        self._model_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._model_combo.setMinimumContentsLength(24)
        self._model_combo.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        for model in MODELS:
            self._model_combo.addItem(tr(model.label_key), model.model_id)
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)
        self._size_combo = QComboBox()
        self._size_combo.setObjectName("sizeCombo")
        self._size_combo.addItems(SIZES)
        self._size_combo.currentIndexChanged.connect(self._persist_parameter_choices)
        layout.addWidget(QLabel(tr("main.field.api_preset")))
        layout.addWidget(self._preset_combo)
        layout.addWidget(QLabel(tr("main.field.model")))
        layout.addWidget(self._model_combo)
        layout.addWidget(QLabel(tr("main.field.output_size")))
        layout.addWidget(self._size_combo)
        layout.addWidget(QLabel(tr("main.field.output_ratio")))
        self._ratio_group = QButtonGroup(self)
        self._ratio_group.setExclusive(True)
        ratio_grid = QGridLayout()
        ratio_grid.setSpacing(5)
        for index, ratio in enumerate(RATIOS):
            button = QPushButton(ratio)
            button.setCheckable(True)
            button.setProperty("ratio", True)
            button.setObjectName(f"ratio_{ratio.replace(':', '_')}")
            self._ratio_group.addButton(button)
            ratio_grid.addWidget(button, index // 3, index % 3)
        self._ratio_group.buttonClicked.connect(self._persist_parameter_choices)
        layout.addLayout(ratio_grid)
        hint = QLabel(tr("main.parameters.note"))
        hint.setWordWrap(True)
        hint.setProperty("hint", True)
        layout.addWidget(hint)
        return frame

    def _build_request_card(self) -> QWidget:
        frame, layout = self._card(tr("main.card.request"))
        self._mode_stack = QStackedWidget()
        self._mode_stack.setObjectName("modeStack")
        self._mode_stack.addWidget(self._build_generation_page())
        self._mode_stack.addWidget(self._build_edit_page())
        layout.addWidget(self._mode_stack)
        self._cancel_btn = QPushButton(tr("main.button.cancel_request"))
        self._cancel_btn.setObjectName("cancelRequestButton")
        self._cancel_btn.setProperty("danger", True)
        self._cancel_btn.clicked.connect(self._cancel_active_job)
        self._cancel_btn.hide()
        layout.addWidget(self._cancel_btn)
        return frame

    def _build_generation_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self._prompt_edit = QPlainTextEdit()
        self._prompt_edit.setObjectName("generationPrompt")
        self._prompt_edit.setPlaceholderText(tr("main.generation.placeholder"))
        self._prompt_edit.setMinimumHeight(126)
        self._gen_btn = QPushButton(tr("main.mode.generate"))
        self._gen_btn.setObjectName("generateButton")
        self._gen_btn.setProperty("primary", True)
        self._gen_btn.clicked.connect(self._on_generate_clicked)
        add_queue = QPushButton(tr("main.button.add_queue"))
        add_queue.setObjectName("addQueueButton")
        add_queue.clicked.connect(self._on_add_queue)
        row = QHBoxLayout()
        row.addWidget(self._gen_btn, 2)
        row.addWidget(add_queue, 1)
        layout.addWidget(self._prompt_edit)
        layout.addLayout(row)
        return page

    def _build_edit_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self._source_label = QLabel(tr("main.source.empty"))
        self._source_label.setObjectName("sourceImageLabel")
        self._source_label.setWordWrap(True)
        import_button = QPushButton(tr("main.button.import_work_image"))
        import_button.setObjectName("importWorkImageButton")
        import_button.clicked.connect(self.import_image)
        source_row = QHBoxLayout()
        source_row.addWidget(self._source_label, 1)
        source_row.addWidget(import_button)
        layout.addLayout(source_row)

        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(True)
        tool_row = QHBoxLayout()
        for text_key, tool in (
            ("main.tool.rectangle", "rect"),
            ("main.tool.brush", "brush"),
            ("main.tool.eraser", "eraser"),
        ):
            button = QPushButton(tr(text_key))
            button.setCheckable(True)
            button.setProperty("tool", True)
            button.setObjectName(f"{tool}ToolButton")
            button.setProperty("toolName", tool)
            self._tool_group.addButton(button)
            tool_row.addWidget(button)
        self._tool_group.buttons()[0].setChecked(True)
        self._tool_group.buttonClicked.connect(
            lambda button: self._preview.set_tool(str(button.property("toolName")))
        )
        undo_button = QPushButton(tr("main.button.undo"))
        undo_button.setObjectName("undoAnnotationButton")
        undo_button.clicked.connect(lambda: self._preview.undo())
        clear_button = QPushButton(tr("main.button.clear"))
        clear_button.setObjectName("clearAnnotationButton")
        clear_button.clicked.connect(self._preview_clear_selection)
        tool_row.addWidget(undo_button)
        tool_row.addWidget(clear_button)
        layout.addLayout(tool_row)
        brush_row = QHBoxLayout()
        brush_row.addWidget(QLabel(tr("main.field.brush_radius")))
        self._brush_slider = QSlider(Qt.Orientation.Horizontal)
        self._brush_slider.setObjectName("brushRadiusSlider")
        self._brush_slider.setRange(3, 160)
        self._brush_slider.setValue(DEFAULT_BRUSH_RADIUS)
        self._brush_radius_label = QLabel(str(DEFAULT_BRUSH_RADIUS))
        self._brush_slider.valueChanged.connect(self._on_brush_radius_changed)
        brush_row.addWidget(self._brush_slider, 1)
        brush_row.addWidget(self._brush_radius_label)
        layout.addLayout(brush_row)
        guide = QLabel(tr("main.annotation.note"))
        guide.setWordWrap(True)
        guide.setProperty("hint", True)
        layout.addWidget(guide)
        self._edit_prompt = QPlainTextEdit()
        self._edit_prompt.setObjectName("editPrompt")
        self._edit_prompt.setPlaceholderText(tr("main.edit.placeholder"))
        self._edit_prompt.setMinimumHeight(104)
        self._edit_btn = QPushButton(tr("main.button.send_edit"))
        self._edit_btn.setObjectName("editButton")
        self._edit_btn.setProperty("primary", True)
        self._edit_btn.clicked.connect(self._on_edit_clicked)
        layout.addWidget(self._edit_prompt)
        layout.addWidget(self._edit_btn)
        return page

    def _build_queue_card(self) -> QWidget:
        frame, layout = self._card(tr("main.card.queue"))
        self._queue_list = QListWidget()
        self._queue_list.setObjectName("queueList")
        self._queue_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._queue_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._queue_list.customContextMenuRequested.connect(self._queue_context_menu)
        self._queue_list.setMinimumHeight(135)
        layout.addWidget(self._queue_list)
        self._queue_start_btn = QPushButton(tr("main.queue.start"))
        self._queue_start_btn.setObjectName("startQueueButton")
        self._queue_start_btn.clicked.connect(self._on_start_queue)
        self._queue_retry_btn = QPushButton(tr("main.queue.retry"))
        self._queue_retry_btn.setObjectName("retryQueueButton")
        self._queue_retry_btn.clicked.connect(self._on_retry_queue)
        self._queue_stop_btn = QPushButton(tr("main.queue.stop"))
        self._queue_stop_btn.setObjectName("stopQueueButton")
        self._queue_stop_btn.clicked.connect(self._on_stop_queue)
        self._queue_clear_btn = QPushButton(tr("main.queue.clear"))
        self._queue_clear_btn.setObjectName("clearQueueButton")
        self._queue_clear_btn.clicked.connect(self._on_clear_queue)
        row = QGridLayout()
        row.addWidget(self._queue_start_btn, 0, 0)
        row.addWidget(self._queue_retry_btn, 0, 1)
        row.addWidget(self._queue_stop_btn, 1, 0)
        row.addWidget(self._queue_clear_btn, 1, 1)
        layout.addLayout(row)
        return frame

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(285)
        panel.setMaximumWidth(450)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self._text_panel = TextResultPanel()
        self._log_panel = LogPanel()
        layout.addWidget(self._text_panel, 2)
        layout.addWidget(self._log_panel, 3)
        return panel

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #080d17; color: #dbe5f6; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
            QMenuBar { background: #080d17; color: #9aa9c4; }
            QMenuBar::item:selected, QMenu::item:selected { background: #253452; color: #f5d547; }
            QMenu { background: #101827; border: 1px solid #31415f; }
            QFrame#appHeader { background: #101827; border: 1px solid #2a3853; border-radius: 11px; }
            QLabel#brandMark { color: #0b1020; background: #f5d547; border-radius: 8px; font-weight: 900; padding: 5px 10px; }
            QLabel#brandTitle { color: #f4f7ff; font-size: 20px; font-weight: 800; }
            QLabel#brandSubtitle, QLabel[hint="true"], QLabel#sourceImageLabel { color: #8f9fba; }
            QFrame[card="true"] { background: #101827; border: 1px solid #2a3853; border-radius: 10px; }
            QLabel[cardTitle="true"] { color: #f3f6ff; font-size: 14px; font-weight: 700; }
            QLineEdit, QPlainTextEdit, QComboBox, QListWidget { background: #090f1b; border: 1px solid #2d3c59; border-radius: 7px; padding: 7px; selection-background-color: #526acb; }
            QPlainTextEdit:focus, QComboBox:focus, QListWidget:focus { border-color: #7b70ff; }
            QPushButton { background: #172238; color: #c8d3e8; border: 1px solid #344462; border-radius: 7px; padding: 7px 10px; }
            QPushButton:hover { background: #21304b; border-color: #56688a; }
            QPushButton:pressed { background: #111a2b; }
            QPushButton:disabled { color: #59667d; background: #101725; border-color: #232d40; }
            QPushButton[primary="true"] { background: #6e58d9; border-color: #8c79ef; color: white; font-weight: 700; padding: 9px; }
            QPushButton[primary="true"]:hover { background: #7d67e7; }
            QPushButton[danger="true"] { background: #3b1d2a; border-color: #8f4058; color: #ff9bab; }
            QPushButton[segment="true"]:checked, QPushButton[ratio="true"]:checked, QPushButton[tool="true"]:checked { background: #2d2855; border-color: #a693ff; color: #f5d547; }
            QSlider::groove:horizontal { height: 4px; background: #2d3a54; border-radius: 2px; }
            QSlider::handle:horizontal { width: 15px; margin: -6px 0; background: #f5d547; border-radius: 7px; }
            QScrollArea#controlScroll { background: transparent; }
            QStatusBar { background: #0d1422; color: #8697b5; }
            QSplitter::handle { background: transparent; width: 7px; }
            """
        )

    # ------------------------------------------------------------- settings adapter
    def _setting(self, key: str, default: Any = None) -> Any:
        service = self._settings_service
        if service is None:
            return default
        getter = getattr(service, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except TypeError:
                try:
                    value = getter(key)
                    return default if value is None else value
                except Exception:
                    pass
        return getattr(service, key, default)

    def _write_setting(self, key: str, value: Any) -> bool:
        service = self._settings_service
        if service is None:
            return False
        setter = getattr(service, "set", None)
        try:
            if callable(setter):
                setter(key, value)
                return True
            if hasattr(service, key):
                setattr(service, key, value)
                return True
        except (TypeError, ValueError, RuntimeError, OSError) as exc:
            self._log("warn", tr("main.settings.save_failed", error=user_error_text(exc)))
        return False

    def _load_preferences(self) -> None:
        model_index = int(self._setting("last_model_index", 0) or 0)
        size_index = int(self._setting("last_size_index", 0) or 0)
        ratio = str(self._setting("last_ratio", DEFAULT_RATIO) or DEFAULT_RATIO)
        self._model_combo.setCurrentIndex(max(0, min(len(MODELS) - 1, model_index)))
        preferred_size = SIZES[max(0, min(len(SIZES) - 1, size_index))]
        self._refresh_supported_sizes(preferred_size)
        button = next((item for item in self._ratio_group.buttons() if item.text() == ratio), None)
        (button or self._ratio_group.buttons()[0]).setChecked(True)

    def _persist_parameter_choices(self, *_args: Any) -> None:
        if not hasattr(self, "_model_combo"):
            return
        self._write_setting("last_model_index", self._model_combo.currentIndex())
        current_size = self._size_combo.currentText()
        size_index = SIZES.index(current_size) if current_size in SIZES else 0
        self._write_setting("last_size_index", size_index)
        self._write_setting("last_ratio", self._selected_ratio())

    def _on_model_changed(self, *_args: Any) -> None:
        self._refresh_supported_sizes(self._size_combo.currentText())
        self._persist_parameter_choices()

    def _refresh_supported_sizes(self, preferred_size: str | None = None) -> None:
        """Expose only output sizes supported by the selected model."""

        model = self._current_model()
        selected = str(preferred_size or self._size_combo.currentText() or "")
        allowed = tuple(size for size in SIZES if size in model.supported_sizes) or ("1K",)
        self._size_combo.blockSignals(True)
        try:
            self._size_combo.clear()
            self._size_combo.addItems(allowed)
            self._size_combo.setCurrentText(selected if selected in allowed else allowed[0])
        finally:
            self._size_combo.blockSignals(False)

    def _available_presets(self) -> tuple[ApiPreset, ...]:
        if self._console_runtime_presets is not None:
            return self._console_runtime_presets
        service = self._settings_service
        if service is None:
            return ()
        raw = getattr(service, "presets", None)
        if callable(raw):
            raw = raw()
        if raw is None:
            raw = getattr(service, "api_presets", ())
            if callable(raw):
                raw = raw()
        result: list[ApiPreset] = []
        for item in raw or ():
            if isinstance(item, ApiPreset):
                result.append(item)
            elif isinstance(item, dict):
                preset_id = item.get("preset_id", item.get("id"))
                if preset_id and item.get("provider"):
                    result.append(
                        ApiPreset(
                            preset_id=str(preset_id),
                            name=str(item.get("name") or preset_id),
                            provider=str(item["provider"]),
                            credential_ref=str(item.get("credential_ref") or ""),
                        )
                    )
        return tuple(result)

    def _refresh_presets(self) -> None:
        current = str(self._preset_combo.currentData() or "") if self._preset_combo.count() else ""
        active = str(self._setting("active_preset_id", "") or current)
        presets = self._available_presets()
        self._preset_combo.blockSignals(True)
        self._preset_combo.clear()
        for preset in presets:
            self._preset_combo.addItem(f"{preset.name} · {preset.provider}", preset.preset_id)
        wanted = active or current
        index = self._preset_combo.findData(wanted)
        if index >= 0:
            self._preset_combo.setCurrentIndex(index)
        self._preset_combo.blockSignals(False)
        if not presets:
            self._preset_combo.addItem(tr("main.preset.add_first"), "")

    def _on_preset_changed(self, *_args: Any) -> None:
        preset_id = str(self._preset_combo.currentData() or "")
        if preset_id:
            self._write_setting("active_preset_id", preset_id)

    def _active_preset(self) -> _PresetSnapshot | None:
        preset_id = str(self._preset_combo.currentData() or "")
        return self._preset_snapshot(preset_id)

    def _preset_snapshot(self, preset_id: str) -> _PresetSnapshot | None:
        preset = next((item for item in self._available_presets() if item.preset_id == preset_id), None)
        if preset is None:
            return None
        provider = API_PROVIDERS.get(preset.provider)
        if provider is None:
            return None
        return _PresetSnapshot(
            preset_id=preset.preset_id,
            name=preset.name,
            provider=preset.provider,
            credential_ref=preset.credential_ref,
            api_url=provider.api_url,
        )

    def _resolve_api_key(self, preset_id: str) -> str | None:
        """Resolve a credential only at dispatch time; never cache or log it."""
        service = self._settings_service
        if service is None:
            return None
        for name in ("get_api_key", "resolve_api_key", "get_credential_for_preset"):
            method = getattr(service, name, None)
            if callable(method):
                try:
                    value = method(preset_id)
                except Exception as exc:
                    raise RuntimeError(
                        tr("main.credential.read_failed", error=user_error_text(exc))
                    ) from exc
                return str(value) if value else None
        # Test fakes may intentionally expose this compatibility property.  Never
        # inspect a preset mapping for api_key.
        value = getattr(service, "api_key", None)
        return str(value) if value else None

    # -------------------------------------------------------------- service adapter
    def _bind_service_signals(self) -> None:
        service = self._image_service
        if service is None:
            return
        bindings = {
            "success": self._on_service_success,
            "finished": self._on_service_success,
            "error": self._on_service_error,
            "text_only": self._on_service_text_only,
            "cancelled": self._on_service_cancelled,
            "progress": self._on_service_progress,
            "log": self._on_service_log,
        }
        connected: set[int] = set()
        for name, slot in bindings.items():
            signal = getattr(service, name, None)
            if signal is None or not hasattr(signal, "connect") or id(signal) in connected:
                continue
            try:
                signal.connect(slot)
                connected.add(id(signal))
            except (TypeError, RuntimeError):
                pass

    def _service_busy(self) -> bool:
        service = self._image_service
        if service is None:
            return False
        busy = getattr(service, "busy", None)
        if callable(busy):
            try:
                return bool(busy())
            except TypeError:
                pass
        if busy is not None:
            return bool(busy)
        is_running = getattr(service, "is_running", None)
        if callable(is_running):
            return bool(is_running())
        return self._current_job is not None

    def _dispatch_generation(self, context: _JobContext, api_key: str) -> tuple[bool, str | None, str]:
        request = context.request
        assert isinstance(request, GenerationRequest)
        kwargs = {
            "provider": context.preset.provider,
            "api_key": api_key,
            "model_id": request.model_id,
            "prompt": request.prompt,
            "image_size": request.size,
            "aspect_ratio": request.ratio,
            "api_url": context.preset.api_url,
        }
        return self._invoke_start("generation", request, kwargs)

    def _dispatch_edit(self, context: _JobContext, api_key: str) -> tuple[bool, str | None, str]:
        request = context.request
        assert isinstance(request, EditRequest)
        kwargs = {
            "provider": context.preset.provider,
            "api_key": api_key,
            "model_id": request.model_id,
            "edit_prompt": request.edit_prompt,
            "original_image": request.source_image_bytes,
            "annotated_image": request.annotated_image_bytes,
            "image_size": request.size,
            "aspect_ratio": request.ratio,
            "api_url": context.preset.api_url,
        }
        return self._invoke_start("edit", request, kwargs)

    def _invoke_start(
        self,
        kind: Literal["generation", "edit"],
        request: GenerationRequest | EditRequest,
        concrete_kwargs: dict[str, Any],
    ) -> tuple[bool, str | None, str]:
        service = self._image_service
        if service is None:
            return False, None, tr("main.service.not_initialized")
        method = getattr(service, "start_generation" if kind == "generation" else "start_edit", None)
        if not callable(method) and kind == "generation":
            method = getattr(service, "start", None)
        if not callable(method):
            operation = tr(
                "main.operation.generate" if kind == "generation" else "main.operation.edit"
            )
            return False, None, tr("main.service.unsupported", operation=operation)

        callbacks = {
            "on_log": self._on_service_log,
            "on_progress": self._on_service_progress,
            "on_finished": self._on_service_success,
            "on_error": self._on_service_error,
            "on_text_only": self._on_service_text_only,
        }
        try:
            signature = inspect.signature(method)
            parameters = signature.parameters
            has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
            if has_var_kw or any(name in parameters for name in concrete_kwargs):
                call_kwargs = {name: value for name, value in concrete_kwargs.items() if has_var_kw or name in parameters}
                # Callback-style services receive the same terminal adapters.
                call_kwargs.update({name: value for name, value in callbacks.items() if name in parameters})
                if "request" in parameters:
                    call_kwargs["request"] = request
                if "api_url" in parameters and "api_url" not in call_kwargs:
                    call_kwargs["api_url"] = concrete_kwargs["api_url"]
                result = method(**call_kwargs)
            elif len(parameters) == 1:
                result = method(request)
            else:
                result = method(request=request, **callbacks)
        except Exception as exc:
            return False, None, user_error_text(exc)
        accepted = True if result is None else bool(getattr(result, "accepted", result))
        job_id = getattr(result, "job_id", None)
        reason = str(getattr(result, "reason", "") or "")
        return accepted, str(job_id) if job_id else None, reason

    # --------------------------------------------------------------- request setup
    def _selected_ratio(self) -> str:
        button = self._ratio_group.checkedButton()
        return button.text() if button is not None else DEFAULT_RATIO

    def _current_model(self) -> Any:
        return MODELS[max(0, self._model_combo.currentIndex())]

    def _generation_request(self, prompt: str, preset: _PresetSnapshot) -> GenerationRequest:
        model = self._current_model()
        return GenerationRequest(
            prompt=prompt,
            model_id=model.model_id,
            model_short_name=model.short_name,
            size=self._size_combo.currentText(),
            ratio=self._selected_ratio(),
            preset_id=preset.preset_id,
            provider=preset.provider,
        )

    def _confirm_request(
        self,
        request: GenerationRequest | EditRequest,
        preset: _PresetSnapshot,
        *,
        source: QImage | None = None,
        guide: bytes | None = None,
    ) -> bool:
        dialog = PreflightDialog(
            request,
            self,
            preset_name=preset.name,
            source_image=source,
            annotated_image=guide,
        )
        return dialog.exec() == PreflightDialog.DialogCode.Accepted

    def _on_generate_clicked(self) -> None:
        self.begin_generation()

    def begin_generation(self, *, confirm: bool | None = None) -> bool:
        """Start text generation immediately; ``confirm`` is ignored for API compatibility."""
        del confirm
        prompt = self._prompt_edit.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(
                self,
                tr("main.error.prompt.title"),
                tr("main.error.prompt.generate"),
            )
            self._prompt_edit.setFocus()
            return False
        preset = self._active_preset()
        if preset is None:
            QMessageBox.warning(
                self,
                tr("main.error.api.title"),
                tr("main.error.api.add_and_select"),
            )
            return False
        request = self._generation_request(prompt, preset)
        return self._start_context(_JobContext("generation", request, preset))

    def _on_edit_clicked(self) -> None:
        self.begin_edit()

    def begin_edit(self) -> bool:
        if not self._work_bytes or not self._preview.has_image():
            QMessageBox.warning(
                self,
                tr("main.error.source.title"),
                tr("main.error.source.body"),
            )
            return False
        if not self._preview.has_selection():
            QMessageBox.warning(
                self,
                tr("main.error.selection.title"),
                tr("main.error.selection.body"),
            )
            return False
        prompt = self._edit_prompt.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(
                self,
                tr("main.error.edit_prompt.title"),
                tr("main.error.edit_prompt.body"),
            )
            self._edit_prompt.setFocus()
            return False
        preset = self._active_preset()
        if preset is None:
            QMessageBox.warning(
                self,
                tr("main.error.api.title"),
                tr("main.error.api.add_and_select"),
            )
            return False
        annotated = self._preview.get_annotated_bytes()
        if not annotated or not annotated.startswith(b"\x89PNG\r\n\x1a\n"):
            QMessageBox.warning(
                self,
                tr("main.error.annotation.title"),
                tr("main.error.annotation.body"),
            )
            return False
        model = self._current_model()
        detected_source_fmt = detect_image_format(self._work_bytes)
        wire_source_fmt = "png" if detected_source_fmt == "bmp" else detected_source_fmt
        request = EditRequest(
            source_image_bytes=self._work_bytes,
            annotated_image_bytes=annotated,
            edit_prompt=prompt,
            model_id=model.model_id,
            model_short_name=model.short_name,
            size=self._size_combo.currentText(),
            ratio=self._selected_ratio(),
            preset_id=preset.preset_id,
            provider=preset.provider,
            source_fmt=self._work_fmt,
            source_dpi=self._work_source_dpi,
            wire_source_fmt=wire_source_fmt,
        )
        if not self._confirm_request(
            request,
            preset,
            source=self._preview.image(),
            guide=annotated,
        ):
            return False
        return self._start_context(_JobContext("edit", request, preset))

    def _start_context(self, context: _JobContext) -> bool:
        if self._current_job is not None or self._service_busy():
            self._log("warn", tr("main.job.already_running"))
            return False
        try:
            api_key = self._resolve_api_key(context.preset.preset_id)
        except RuntimeError as exc:
            self._terminal_message("error", user_error_text(exc))
            return False
        if not api_key:
            self._terminal_message("error", tr("main.credential.missing"))
            return False

        self._current_job = context
        self._refresh_busy_ui()
        self._text_panel.clear()
        operation = tr(
            "main.operation.generate"
            if context.kind == "generation"
            else "main.operation.edit"
        )
        self._preview.set_status(tr("main.job.running", operation=operation))
        self._log(
            "info",
            tr(
                "main.job.started",
                operation=operation,
                model=context.request.model_short_name,
                size=context.request.size,
                ratio=context.request.ratio,
            ),
        )
        accepted, job_id, reason = (
            self._dispatch_generation(context, api_key)
            if context.kind == "generation"
            else self._dispatch_edit(context, api_key)
        )
        # A fake or a validation failure may terminate synchronously.
        if self._current_job is context:
            context.job_id = job_id
        if not accepted and self._current_job is context:
            self._current_job = None
            self._refresh_busy_ui()
            self._terminal_message(
                "error",
                tr("main.job.not_started", reason=reason or tr("main.service.busy")),
            )
            return False
        return accepted

    # --------------------------------------------------------------- terminal slots
    def _parse_job_payload(self, args: tuple[Any, ...]) -> tuple[str | None, Any]:
        if len(args) >= 2:
            return str(args[0]) if args[0] is not None else None, args[1]
        return None, args[0] if args else None

    def _job_matches(self, job_id: str | None) -> bool:
        context = self._current_job
        return context is not None and (
            not job_id or not context.job_id or context.job_id == job_id
        )

    def _on_service_success(self, *args: Any) -> None:
        job_id, payload = self._parse_job_payload(args)
        if not self._job_matches(job_id):
            return
        context = self._current_job
        assert context is not None
        try:
            result = self._normalise_result(context, payload)
            if not result.image_bytes or QImage.fromData(result.image_bytes).isNull():
                raise ValueError(tr("main.result.decode_failed"))
        except Exception as exc:
            self._finish_context("failed", message=user_error_text(exc))
            return
        self._show_result(result)
        self._finish_context("done", result=result)

    def _normalise_result(
        self,
        context: _JobContext,
        payload: Any,
    ) -> GenerationResult | EditResult:
        if isinstance(payload, (GenerationResult, EditResult)):
            payload.text_content = redact_for_display(payload.text_content)
            if context.kind == "generation" and isinstance(payload, GenerationResult):
                dpi = T2I_DPI_BY_SIZE.get(context.request.size)
                return (
                    self._apply_output_encoding(payload, dpi=dpi, target_fmt=payload.fmt)
                    if dpi
                    else payload
                )
            if context.kind == "edit" and isinstance(payload, EditResult):
                request = context.request
                assert isinstance(request, EditRequest)
                return self._apply_output_encoding(
                    payload,
                    dpi=EDIT_OUTPUT_DPI,
                    target_fmt=request.source_fmt,
                    jpeg_quality=95,
                )
            raise ValueError(tr("main.result.type_mismatch"))
        image = getattr(payload, "image", None)
        if image is None and isinstance(payload, dict):
            image = payload.get("image")
        data = getattr(image, "data", None) if image is not None else None
        if data is None and isinstance(image, dict):
            data = image.get("data")
        if data is None:
            data = getattr(payload, "image_bytes", None)
        if data is None:
            raise ValueError(tr("main.result.no_image"))
        fmt = getattr(image, "fmt", None) or getattr(payload, "fmt", None) or detect_image_format(bytes(data))
        width = int(getattr(image, "width", 0) or getattr(payload, "width", 0) or 0)
        height = int(getattr(image, "height", 0) or getattr(payload, "height", 0) or 0)
        text = redact_for_display(
            getattr(payload, "text", "") or getattr(payload, "text_content", "") or ""
        )
        usage = TokenUsage(
            int(getattr(payload, "input_tokens", 0) or 0),
            int(getattr(payload, "output_tokens", 0) or 0),
        )
        if context.kind == "generation":
            request = context.request
            assert isinstance(request, GenerationRequest)
            dpi = T2I_DPI_BY_SIZE.get(request.size)
            result = GenerationResult(
                bytes(data), str(fmt), width, height, request, text_content=text,
                usage=usage,
            )
            return self._apply_output_encoding(result, dpi=dpi, target_fmt=str(fmt)) if dpi else result
        request = context.request
        assert isinstance(request, EditRequest)
        result = EditResult(
            bytes(data), str(fmt), width, height, request, text_content=text,
            usage=usage,
        )
        return self._apply_output_encoding(
            result,
            dpi=EDIT_OUTPUT_DPI,
            target_fmt=request.source_fmt,
            jpeg_quality=95,
        )

    def _apply_output_encoding(
        self,
        result: GenerationResult | EditResult,
        *,
        dpi: float,
        target_fmt: str,
        jpeg_quality: int = -1,
    ) -> GenerationResult | EditResult:
        """Embed DPI and, for edits, restore the source container format.

        A failed encoder never invalidates an otherwise valid provider image and
        never leaves metadata claiming that DPI was written.
        """
        try:
            return self._encode_output_image(
                result,
                dpi=dpi,
                target_fmt=target_fmt,
                jpeg_quality=jpeg_quality,
            )
        except Exception as exc:
            result.output_dpi = None
            self._log("warn", tr("main.encode.failed", error=user_error_text(exc)))
            return result

    def _encode_output_image(
        self,
        result: GenerationResult | EditResult,
        *,
        dpi: float,
        target_fmt: str,
        jpeg_quality: int,
    ) -> GenerationResult | EditResult:
        fmt = str(target_fmt or result.fmt).lower().removeprefix("image/")
        fmt = "jpeg" if fmt in {"jpg", "jpeg"} else fmt
        supported = {
            bytes(value).decode("ascii", "ignore").lower()
            for value in QImageWriter.supportedImageFormats()
        }
        if fmt not in {"png", "jpeg", "webp", "bmp"} or fmt not in supported:
            self._log("warn", tr("main.encode.unsupported", format=fmt.upper()))
            result.output_dpi = None
            return result
        image = QImage.fromData(result.image_bytes)
        if image.isNull():
            result.output_dpi = None
            return result
        dots_per_meter = max(1, int(round(float(dpi) / 0.0254)))
        image.setDotsPerMeterX(dots_per_meter)
        image.setDotsPerMeterY(dots_per_meter)
        if fmt == "jpeg" and image.hasAlphaChannel():
            flattened = QImage(image.size(), QImage.Format.Format_RGB32)
            flattened.fill(Qt.GlobalColor.white)
            painter = QPainter(flattened)
            painter.drawImage(0, 0, image)
            painter.end()
            image = flattened
            image.setDotsPerMeterX(dots_per_meter)
            image.setDotsPerMeterY(dots_per_meter)
        data = QByteArray()
        buffer = QBuffer(data)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            result.output_dpi = None
            self._log("warn", tr("main.encode.buffer_failed"))
            return result
        quality = jpeg_quality if fmt == "jpeg" else -1
        ok = (
            image.save(buffer, fmt.upper(), quality)
            if quality >= 0
            else image.save(buffer, fmt.upper())
        )
        buffer.close()
        if not ok or not data:
            result.output_dpi = None
            self._log("warn", tr("main.encode.format_failed", format=fmt.upper()))
            return result
        encoded_bytes = bytes(data)
        verified = QImage.fromData(encoded_bytes)
        if verified.isNull():
            result.output_dpi = None
            self._log("warn", tr("main.encode.verify_failed", format=fmt.upper()))
            return result
        actual_dpi = (
            verified.dotsPerMeterX() * 0.0254,
            verified.dotsPerMeterY() * 0.0254,
        )
        if not all(value > 0 for value in actual_dpi):
            readback_dpi: tuple[float, float] | None = None
        elif all(abs(value - float(dpi)) <= 0.1 for value in actual_dpi):
            # Dots-per-metre is integral, so nominal 300 DPI commonly reads
            # back as 299.9994.  Preserve the requested nominal value only
            # within that serialization tolerance.
            readback_dpi = (float(dpi), float(dpi))
        else:
            readback_dpi = tuple(round(value, 4) for value in actual_dpi)
            self._log(
                "warn",
                tr(
                    "main.encode.dpi_not_preserved",
                    format=fmt.upper(),
                    requested=float(dpi),
                    actual_x=readback_dpi[0],
                    actual_y=readback_dpi[1],
                ),
            )
        result.image_bytes = encoded_bytes
        result.fmt = fmt
        result.width = verified.width()
        result.height = verified.height()
        result.output_dpi = readback_dpi
        return result

    def _on_service_error(self, *args: Any) -> None:
        job_id, message = self._parse_job_payload(args)
        if not self._job_matches(job_id):
            return
        self._finish_context(
            "failed",
            message=user_error_text(message or tr("main.error.unknown")),
        )

    def _on_service_text_only(self, *args: Any) -> None:
        job_id, text = self._parse_job_payload(args)
        if not self._job_matches(job_id):
            return
        response = redact_for_display(text or "")
        self._text_panel.set_text(response)
        self._log(
            "warn",
            tr("main.text_only.log", response=response.replace(chr(10), " ")[:120]),
        )
        self._finish_context(
            "failed",
            message=tr("main.text_only.status"),
            preserve_text=True,
        )

    def _on_service_cancelled(self, *args: Any) -> None:
        job_id = str(args[0]) if args else None
        if not self._job_matches(job_id):
            return
        self._finish_context("cancelled", message=tr("main.request.cancelled"))

    def _on_service_progress(self, *args: Any) -> None:
        if len(args) >= 3:
            job_id, received, total = args[0], int(args[1]), int(args[2])
            if not self._job_matches(str(job_id)):
                return
            if total > 0:
                text = tr("main.progress.percent", percent=received / total * 100)
            else:
                text = tr("main.progress.kib", kib=received / 1024)
        elif args:
            text = str(args[-1])
        else:
            return
        self._preview.set_status(tr("main.progress.processing", progress=text))
        self._job_status_label.setText(text)

    def _on_service_log(self, *args: Any) -> None:
        if len(args) >= 2:
            self._log(str(args[-2]), str(args[-1]))
        elif args:
            self._log("info", str(args[0]))

    def _finish_context(
        self,
        outcome: Literal["done", "failed", "cancelled"],
        *,
        result: GenerationResult | EditResult | None = None,
        message: str = "",
        preserve_text: bool = False,
    ) -> None:
        context = self._current_job
        if context is None:
            return
        message = redact_for_display(user_error_text(message))
        self._current_job = None
        self._refresh_busy_ui()
        if outcome == "done":
            self._preview.set_status(tr("main.request.done"))
            self._log("success", tr("main.request.completed"))
            self._increment_daily_count()
            if result is not None:
                self._store_result(result, automatic=True)
        elif outcome == "cancelled":
            self._preview.set_status(tr("main.request.cancelled"))
            self._log("warn", message or tr("main.request.cancelled_sentence"))
        else:
            self._preview.set_status(tr("main.request.failed_status", message=message))
            self._log("error", tr("main.request.failed_log", message=message))
            if not preserve_text:
                self._text_panel.set_text(message)

        if context.queue_task is not None:
            self._queue_terminal(context.queue_task, outcome, message)
        if self._close_when_idle:
            self._mark_pending_cancelled()
            QTimer.singleShot(0, self.close)

    def _show_result(self, result: GenerationResult | EditResult) -> None:
        result.text_content = redact_for_display(result.text_content)
        self._last_result = result
        self._work_bytes = bytes(result.image_bytes)
        self._work_fmt = result.fmt
        self._work_source_dpi = result.output_dpi
        self._set_saved_path(result.saved_path)
        self._preview.set_image(self._work_bytes)
        self._preview.clear_selection()
        self._source_label.setText(
            tr(
                "main.source.current",
                width=result.width or self._preview.image().width(),
                height=result.height or self._preview.image().height(),
                format=result.fmt.upper(),
            )
        )
        self._text_panel.set_text(result.text_content, expand=bool(result.text_content))

    # ---------------------------------------------------------------------- queue
    def _on_add_queue(self) -> None:
        prompt = self._prompt_edit.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(
                self,
                tr("main.error.prompt.title"),
                tr("main.queue.prompt_required"),
            )
            return
        preset = self._active_preset()
        if preset is None:
            QMessageBox.warning(
                self,
                tr("main.error.api.title"),
                tr("main.queue.preset_required"),
            )
            return
        task = QueuedTask(
            prompt=prompt,
            preset_id=preset.preset_id,
            model_index=self._model_combo.currentIndex(),
            size_index=SIZES.index(self._size_combo.currentText()),
            ratio=self._selected_ratio(),
        )
        self._queue.append(task)
        self._queue_presets[id(task)] = preset
        self._log("info", tr("main.queue.added", prompt=prompt[:60]))
        self._refresh_queue()

    def _on_start_queue(self) -> None:
        if self._queue_running:
            return
        if self._current_job is not None or self._service_busy():
            self._log("warn", tr("main.queue.job_running"))
            return
        if not any(task.status == QueueStatus.PENDING for task in self._queue):
            self._log("warn", tr("main.queue.no_pending"))
            return
        self._queue_running = True
        self._queue_consecutive_failures = 0
        self._refresh_queue()
        self._queue_run_next()

    def _queue_run_next(self) -> None:
        if not self._queue_running or self._current_job is not None or self._service_busy():
            return
        index = next(
            (idx for idx, task in enumerate(self._queue) if task.status == QueueStatus.PENDING),
            -1,
        )
        if index < 0:
            self._queue_running = False
            self._queue_running_idx = -1
            self._log("success", tr("main.queue.completed"))
            self._refresh_queue()
            return
        task = self._queue[index]
        display_snapshot = self._queue_presets.get(id(task))
        preset = self._preset_snapshot(task.preset_id)
        if display_snapshot is None or preset is None:
            self._queue_terminal(
                task,
                "failed",
                tr("main.queue.snapshot_missing"),
            )
            return
        model_index = max(0, min(len(MODELS) - 1, task.model_index))
        size_index = max(0, min(len(SIZES) - 1, task.size_index))
        model = MODELS[model_index]
        if SIZES[size_index] not in model.supported_sizes:
            self._queue_terminal(
                task,
                "failed",
                tr("main.queue.unsupported_size"),
            )
            return
        request = GenerationRequest(
            prompt=task.prompt,
            model_id=model.model_id,
            model_short_name=model.short_name,
            size=SIZES[size_index],
            ratio=task.ratio,
            preset_id=preset.preset_id,
            provider=preset.provider,
        )
        context = _JobContext("generation", request, preset, queue_task=task)
        task.status = QueueStatus.RUNNING
        task.error_message = ""
        self._queue_running_idx = index
        self._refresh_queue()
        accepted = self._start_context(context)
        if not accepted and task.status == QueueStatus.RUNNING:
            self._queue_terminal(
                task,
                "failed",
                tr("main.queue.dispatch_failed"),
            )

    def _queue_terminal(
        self,
        task: QueuedTask,
        outcome: Literal["done", "failed", "cancelled"],
        message: str,
    ) -> None:
        index = next(
            (candidate for candidate, item in enumerate(self._queue) if item is task),
            -1,
        )
        if index < 0:
            self._queue_running = False
            self._queue_running_idx = -1
            self._log("error", tr("main.queue.running_task_missing"))
            self._refresh_queue()
            return
        if outcome == "done":
            task.status = QueueStatus.DONE
            task.error_message = ""
            self._queue_consecutive_failures = 0
        elif outcome == "cancelled":
            task.status = QueueStatus.CANCELLED
            task.error_message = message
            self._queue_running = False
        else:
            task.status = QueueStatus.FAILED
            task.error_message = message
            self._queue_consecutive_failures += 1
            if self._queue_consecutive_failures >= 3:
                self._queue_running = False
                self._log("warn", tr("main.queue.failure_pause"))
        self._queue_running_idx = -1
        self._refresh_queue()
        if self._queue_running:
            # The concrete service reaches IDLE before its terminal signal.  A
            # queued turn also protects callback-style services that clean up at
            # the end of their current signal delivery.
            QTimer.singleShot(0, self._queue_run_next)

    def _on_retry_queue(self) -> None:
        changed = False
        for task in self._queue:
            if task.status in {QueueStatus.FAILED, QueueStatus.CANCELLED}:
                task.status = QueueStatus.PENDING
                task.error_message = ""
                changed = True
        if not changed:
            return
        self._queue_consecutive_failures = 0
        self._refresh_queue()
        if not self._queue_running and self._current_job is None:
            self._on_start_queue()

    def _on_stop_queue(self) -> None:
        if not self._queue_running:
            return
        self._queue_running = False
        self._mark_pending_cancelled()
        if self._current_job and self._current_job.queue_task is not None:
            self._cancel_active_job()
        self._refresh_queue()

    def _mark_pending_cancelled(self) -> None:
        for task in self._queue:
            if task.status == QueueStatus.PENDING:
                task.status = QueueStatus.CANCELLED
                task.error_message = tr("main.queue.stopped")
        self._queue_running = False
        self._refresh_queue()

    def _on_clear_queue(self) -> None:
        kept: list[QueuedTask] = []
        removed: list[QueuedTask] = []
        for task in self._queue:
            (kept if task.status == QueueStatus.RUNNING else removed).append(task)
        self._queue = kept
        for task in removed:
            self._queue_presets.pop(id(task), None)
        self._refresh_queue()

    def _queue_context_menu(self, position: QPoint) -> None:
        item = self._queue_list.itemAt(position)
        if item is None:
            return
        row = self._queue_list.row(item)
        if not 0 <= row < len(self._queue) or self._queue[row].status == QueueStatus.RUNNING:
            return
        menu = QMenu(self)
        remove = menu.addAction(tr("main.queue.remove"))
        retry = menu.addAction(tr("main.queue.requeue"))
        selected = menu.exec(self._queue_list.viewport().mapToGlobal(position))
        if selected == remove:
            task = self._queue.pop(row)
            self._queue_presets.pop(id(task), None)
        elif selected == retry:
            self._queue[row].status = QueueStatus.PENDING
            self._queue[row].error_message = ""
        self._refresh_queue()

    def _refresh_queue(self) -> None:
        self._queue_running_idx = next(
            (
                index
                for index, task in enumerate(self._queue)
                if task.status == QueueStatus.RUNNING
            ),
            -1,
        )
        icons = {
            QueueStatus.PENDING: "○",
            QueueStatus.RUNNING: "▶",
            QueueStatus.DONE: "✓",
            QueueStatus.FAILED: "!",
            QueueStatus.CANCELLED: "×",
        }
        colors = {
            QueueStatus.PENDING: "#9ba8bf",
            QueueStatus.RUNNING: "#f5d547",
            QueueStatus.DONE: "#63d9a5",
            QueueStatus.FAILED: "#ff6b7a",
            QueueStatus.CANCELLED: "#77839a",
        }
        selected = self._queue_list.currentRow()
        self._queue_list.clear()
        for index, task in enumerate(self._queue):
            preset = self._queue_presets.get(id(task))
            suffix = f"  ·  {preset.name}" if preset else ""
            item = QListWidgetItem(f"{icons[task.status]}  {index + 1}. {task.prompt[:42]}{suffix}")
            item.setForeground(QColor(colors[task.status]))
            item.setToolTip(task.error_message or task.prompt)
            self._queue_list.addItem(item)
        if 0 <= selected < self._queue_list.count():
            self._queue_list.setCurrentRow(selected)
        pending = any(task.status == QueueStatus.PENDING for task in self._queue)
        retryable = any(task.status in {QueueStatus.FAILED, QueueStatus.CANCELLED} for task in self._queue)
        self._queue_start_btn.setEnabled(not self._queue_running and pending and self._current_job is None)
        self._queue_retry_btn.setEnabled(not self._queue_running and retryable)
        self._queue_stop_btn.setEnabled(self._queue_running)
        self._queue_clear_btn.setEnabled(bool(self._queue))
        self.queueChanged.emit()

    # --------------------------------------------------------- import/save/metadata
    @staticmethod
    def _encode_import_working_png(image: QImage) -> bytes:
        """Encode an EXIF-normalised working image without further loss."""

        data = QByteArray()
        buffer = QBuffer(data)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            raise ValueError(tr("main.import.normalize_failed"))
        ok = image.save(buffer, "PNG")
        buffer.close()
        encoded = bytes(data)
        if not ok or not encoded or QImage.fromData(encoded).isNull():
            raise ValueError(tr("main.import.normalize_failed"))
        return encoded

    def import_image(self, path: str | bool | None = None) -> bool:
        # QAction/QPushButton triggered(bool) is accepted without treating bool as a path.
        if isinstance(path, bool):
            path = None
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self,
                tr("main.import.title"),
                "",
                tr("main.import.filter"),
            )
        if not path:
            return False
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            QMessageBox.critical(
                self,
                tr("main.import.failed.title"),
                user_error_text(exc),
            )
            return False
        reader = QImageReader(str(path))
        reader.setDecideFormatFromContent(True)
        # Apply JPEG EXIF orientation before the image reaches either preview
        # or the API.  Otherwise the provider may rotate the original while the
        # locally-rendered annotation remains in raw pixel coordinates.
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            QMessageBox.warning(
                self,
                tr("main.import.failed.title"),
                user_error_text(reader.errorString())
                if reader.errorString()
                else tr("main.import.unrecognized"),
            )
            return False
        fmt = bytes(reader.format()).decode("ascii", "ignore").lower() or detect_image_format(data)
        fmt = "jpeg" if fmt in {"jpg", "jpeg"} else fmt
        work_data = data
        archive_fmt = fmt
        if reader.transformation() != QImageIOHandler.Transformation.TransformationNone:
            try:
                work_data = self._encode_import_working_png(image)
            except ValueError as exc:
                QMessageBox.warning(
                    self,
                    tr("main.import.failed.title"),
                    user_error_text(exc),
                )
                return False
            archive_fmt = "png"
        dpi_x = image.dotsPerMeterX() * 0.0254 if image.dotsPerMeterX() > 0 else 0.0
        dpi_y = image.dotsPerMeterY() * 0.0254 if image.dotsPerMeterY() > 0 else 0.0
        self._work_bytes = work_data
        # Preserve the user's source container as the edit-output target even
        # when the wire/annotation coordinate source is a normalised PNG.
        self._work_fmt = fmt
        self._work_source_dpi = (dpi_x, dpi_y) if dpi_x > 0 and dpi_y > 0 else None
        self._set_saved_path(None)
        self._last_result = None
        self._preview.set_image(image)
        details = tr(
            "main.source.details",
            name=Path(path).name,
            width=image.width(),
            height=image.height(),
            format=self._work_fmt.upper(),
        )
        self._source_label.setText(details)
        saved_path = self._store_imported(work_data, archive_fmt)
        if saved_path:
            self._set_saved_path(saved_path)
            self._source_label.setText(
                tr("main.source.archived", details=details, path=saved_path)
            )
            self._source_label.setToolTip(saved_path)
        self._log("success", tr("main.import.succeeded", name=Path(path).name))
        self._set_ui_mode("edit")
        return True

    def _store_result(self, result: GenerationResult | EditResult, *, automatic: bool) -> str | None:
        service = self._storage_service
        if service is None:
            return None
        if isinstance(result, GenerationResult):
            primary = ("auto_save", "save_generation") if automatic else ("save_generation", "auto_save")
        else:
            primary = ("auto_save_edit", "save_edit") if automatic else ("save_edit", "auto_save_edit")
        for name in (*primary, "save_result", "save", "save_image_result"):
            method = getattr(service, name, None)
            if not callable(method):
                continue
            try:
                value = method(result)
            except TypeError:
                try:
                    value = method(result, automatic=automatic)
                except TypeError:
                    continue
            except Exception as exc:
                self._log(
                    "error",
                    tr("main.save.auto_failed", error=user_error_text(exc)),
                )
                return None
            path = str(value) if value else result.saved_path
            if path:
                result.saved_path = path
                self._set_saved_path(path)
                self._source_label.setToolTip(path)
                self._log("success", tr("main.save.succeeded", path=path))
                sidecar_path = Path(path).with_suffix(".json")
                if sidecar_path.is_file():
                    self._log(
                        "success",
                        tr("main.save.sidecar_succeeded", path=sidecar_path),
                    )
            return path
        return None

    def _store_imported(self, data: bytes, fmt: str) -> str | None:
        service = self._storage_service
        method = getattr(service, "save_imported", None) if service is not None else None
        if not callable(method):
            return None
        try:
            value = method(data, fmt)
        except Exception as exc:
            # The decoded working image remains usable even if archival fails.
            self._log(
                "error",
                tr("main.import.archive_failed", error=user_error_text(exc)),
            )
            return None
        path = str(value) if value else ""
        if path:
            self._log("success", tr("main.import.archived", path=path))
        return path or None

    def _set_saved_path(self, path: str | None) -> None:
        self._work_saved_path = str(path) if path else None
        enabled = bool(self._work_saved_path)
        if hasattr(self, "_open_saved_btn"):
            self._open_saved_btn.setEnabled(enabled)
            self._copy_saved_btn.setEnabled(enabled)

    def _open_saved_directory(self) -> None:
        if not self._work_saved_path:
            return
        directory = Path(self._work_saved_path).parent
        if not directory.is_dir():
            QMessageBox.information(
                self,
                tr("main.directory.missing.title"),
                tr("main.directory.save_unavailable", directory=directory),
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def _copy_saved_path(self) -> None:
        if self._work_saved_path:
            QApplication.clipboard().setText(self._work_saved_path)

    # ------------------------------------------------------------ misc UI actions
    def _set_ui_mode(self, mode: Literal["txt2img", "edit"]) -> None:
        self._ui_mode = mode
        self._t2i_mode_btn.setChecked(mode == "txt2img")
        self._edit_mode_btn.setChecked(mode == "edit")
        self._mode_stack.setCurrentIndex(0 if mode == "txt2img" else 1)
        if mode == "edit":
            tool_button = self._tool_group.checkedButton()
            self._preview.set_tool(str(tool_button.property("toolName")) if tool_button else "rect")
            if not self._preview.has_image():
                self._preview.set_status(tr("main.edit.needs_source"))
                self._log("warn", tr("main.edit.entered_without_source"))
        else:
            self._preview.set_tool("view")

    def _on_brush_radius_changed(self, value: int) -> None:
        self._brush_radius_label.setText(str(value))
        self._preview.set_brush_radius(value)

    def _on_selection_changed(self, selected: bool) -> None:
        if selected:
            self._preview.set_status(tr("main.annotation.ready"))

    def _preview_clear_selection(self) -> None:
        self._preview.clear_selection()
        self._preview.set_status(tr("main.annotation.cleared"))

    def _cancel_active_job(self) -> bool:
        context = self._current_job
        service = self._image_service
        if service is None or (context is None and not self._service_busy()):
            return False
        if context is not None:
            context.cancel_requested = True
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setText(tr("main.cancel.cancelling_ellipsis"))
        self._job_status_label.setText(tr("main.cancel.cancelling"))
        method = getattr(service, "cancel", None) or getattr(service, "cancel_current", None)
        if not callable(method):
            self._log("error", tr("main.cancel.unsupported"))
            return False
        try:
            job_id = context.job_id if context else getattr(service, "current_job_id", None)
            try:
                accepted = method(job_id)
            except TypeError:
                accepted = method()
        except Exception as exc:
            self._log("error", tr("main.cancel.failed", error=user_error_text(exc)))
            return False
        return accepted is not False

    def _refresh_busy_ui(self) -> None:
        active = self._current_job is not None
        cancelling = bool(self._current_job and self._current_job.cancel_requested)
        self._gen_btn.setEnabled(not active)
        self._edit_btn.setEnabled(not active)
        self._gen_btn.setText(
            tr("main.job.generating")
            if active and self._current_job.kind == "generation"
            else tr("main.mode.generate")
        )
        self._edit_btn.setText(
            tr("main.job.editing")
            if active and self._current_job.kind == "edit"
            else tr("main.button.send_edit")
        )
        self._cancel_btn.setVisible(active)
        self._cancel_btn.setEnabled(active and not cancelling)
        self._cancel_btn.setText(
            tr("main.cancel.cancelling_ellipsis")
            if cancelling
            else tr("main.button.cancel_request")
        )
        self.import_action.setEnabled(not active)
        self._job_status_label.setText(
            tr("main.job.processing" if active else "main.status.idle")
        )
        self.jobActiveChanged.emit(active)
        if hasattr(self, "_queue_start_btn"):
            self._refresh_queue()

    def _terminal_message(self, level: str, message: str) -> None:
        safe_message = redact_for_display(user_error_text(message))
        self._log(level, safe_message)
        self._preview.set_status(safe_message)
        self._text_panel.set_text(safe_message)

    def _log(self, level: str, message: str) -> None:
        safe_message = redact_for_display(message)
        if hasattr(self, "_log_panel"):
            self._log_panel.append_log(level, safe_message)
        writer = getattr(self._log_service, "write", None)
        if callable(writer):
            try:
                writer(level, safe_message)
            except Exception:
                # Logging is observability, never a reason to recurse or crash UI.
                pass

    def _increment_daily_count(self) -> None:
        service = self._settings_service
        if service is not None:
            method = getattr(service, "increment_generation_count", None)
            if callable(method):
                try:
                    method()
                except Exception as exc:
                    self._log(
                        "warn",
                        tr("main.daily.save_failed", error=user_error_text(exc)),
                    )
        self._update_daily_count()

    def _update_daily_count(self) -> None:
        service = self._settings_service
        count = 0
        if service is not None:
            value = getattr(service, "today_generation_count", None)
            try:
                count = int(value() if callable(value) else value or 0)
            except (TypeError, ValueError):
                count = 0
        self._daily_label.setText(tr("main.daily_count", count=count))

    def _console_access_allowed(self) -> bool:
        auth = self._auth_service
        if auth is None:
            QMessageBox.warning(
                self,
                tr("main.console.verify_unavailable.title"),
                tr("main.console.verify_unavailable.body"),
            )
            return False
        try:
            configured = bool(auth.has_password())
        except Exception as exc:
            QMessageBox.warning(
                self,
                tr("main.console.verify_unavailable.title"),
                user_error_text(exc),
            )
            return False
        if not configured:
            setup = PasswordDialog(
                self,
                title=tr("main.console.setup.title"),
                prompt=tr("main.console.setup.prompt"),
                confirm=True,
                minimum_length=6,
            )
            if setup.exec() != PasswordDialog.DialogCode.Accepted:
                return False
            try:
                auth.set_password(setup.password())
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    tr("main.console.setup_failed.title"),
                    user_error_text(exc),
                )
                return False
            return True
        password, accepted = PasswordDialog.get_password(
            self,
            title=tr("main.console.unlock.title"),
        )
        if not accepted:
            return False
        try:
            valid = bool(auth.verify_password(password))
        except Exception as exc:
            QMessageBox.warning(
                self,
                tr("main.console.verify_failed.title"),
                user_error_text(exc),
            )
            return False
        if not valid:
            QMessageBox.warning(
                self,
                tr("main.console.verify_failed.title"),
                tr("main.console.wrong_password"),
            )
        return valid

    def _open_console(self) -> None:
        if not self._console_access_allowed():
            return
        dialog = ConsoleDialog(
            self._settings_service,
            auth_service=self._auth_service,
            parent=self,
        )
        dialog.presetsChanged.connect(
            lambda: self._set_runtime_console_presets(dialog.presets(), dialog.active_preset_id())
        )
        dialog.exec()
        self._set_runtime_console_presets(dialog.presets(), dialog.active_preset_id())

    def _set_runtime_console_presets(self, presets: tuple[ApiPreset, ...], active_id: str) -> None:
        self._console_runtime_presets = presets
        if active_id:
            self._write_setting("active_preset_id", active_id)
        self._refresh_presets()

    # ---------------------------------------------------------------- safe close
    def closeEvent(self, event: QCloseEvent) -> None:
        active = self._current_job is not None or self._service_busy()
        queued = self._queue_running or any(
            task.status in {QueueStatus.PENDING, QueueStatus.RUNNING} for task in self._queue
        )
        if not active and not queued:
            event.accept()
            return
        if not self._close_confirmed:
            reply = QMessageBox.question(
                self,
                tr("main.close.confirm.title"),
                tr("main.close.confirm.body"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._close_confirmed = True
        self._close_when_idle = True
        self._queue_running = False
        self._mark_pending_cancelled()
        if active:
            event.ignore()
            cancelled = self._cancel_active_job()
            # Concrete ImageService cancellation emits synchronously after fully
            # releasing QNetworkReply.  Callback services may complete later.
            if cancelled and self._current_job is None and not self._service_busy():
                QTimer.singleShot(0, self.close)
            return
        event.accept()


__all__ = ["MainWindow"]
