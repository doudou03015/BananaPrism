"""Immutable request confirmation dialog.

The exact request instance shown by this dialog is exposed through ``request``;
MainWindow submits that same object after acceptance, avoiding a hidden mismatch
between visible preflight values and the network payload.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from banana_prism.i18n import tr
from banana_prism.models import EditRequest, GenerationRequest


class PreflightDialog(QDialog):
    def __init__(
        self,
        request: GenerationRequest | EditRequest,
        parent: QWidget | None = None,
        *,
        preset_name: str = "",
        source_image: QImage | bytes | None = None,
        annotated_image: QImage | bytes | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("preflightDialog")
        self.setWindowTitle(tr("preflight.title"))
        self.setModal(True)
        self.resize(720, 590)
        self.request = request
        self.summary = self._build_summary(request, preset_name)

        heading = QLabel(tr("preflight.heading"))
        heading.setObjectName("preflightHeading")
        subheading = QLabel(tr("preflight.subheading"))
        subheading.setObjectName("preflightSubheading")

        form_widget = QFrame()
        form_widget.setObjectName("summaryCard")
        form = QFormLayout(form_widget)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        for label, value in self.summary.items():
            value_label = QLabel(value)
            value_label.setObjectName(f"preflight_{label}")
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value_label.setWordWrap(True)
            form.addRow(label, value_label)

        prompt = request.edit_prompt if isinstance(request, EditRequest) else request.prompt
        prompt_edit = QPlainTextEdit(prompt)
        prompt_edit.setObjectName("preflightPrompt")
        prompt_edit.setReadOnly(True)
        prompt_edit.setMaximumHeight(130)

        preview_row = QHBoxLayout()
        if isinstance(request, EditRequest):
            source = self._to_image(source_image or request.source_image_bytes)
            guide = self._to_image(annotated_image or request.annotated_image_bytes)
            preview_row.addWidget(self._preview_card(tr("preflight.preview.source"), source))
            preview_row.addWidget(
                self._preview_card(tr("preflight.preview.annotation"), guide)
            )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("preflight.button.send"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("preflight.button.back"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.addWidget(heading)
        content_layout.addWidget(subheading)
        content_layout.addWidget(form_widget)
        content_layout.addWidget(QLabel(tr("preflight.prompt")))
        content_layout.addWidget(prompt_edit)
        if preview_row.count():
            content_layout.addLayout(preview_row)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)
        self.setStyleSheet(
            """
            QDialog { background: #0b1220; color: #dce6f8; }
            QLabel#preflightHeading { font-size: 22px; font-weight: 700; color: #f5d547; }
            QLabel#preflightSubheading { color: #94a4c0; }
            QFrame#summaryCard { background: #111b2d; border: 1px solid #30405f; border-radius: 9px; }
            QPlainTextEdit { background: #080e19; border: 1px solid #30405f; border-radius: 7px; padding: 8px; }
            """
        )

    @staticmethod
    def _build_summary(request: GenerationRequest | EditRequest, preset_name: str) -> dict[str, str]:
        return {
            tr("preflight.field.operation"): tr(
                "preflight.operation.edit"
                if isinstance(request, EditRequest)
                else "preflight.operation.generate"
            ),
            tr("preflight.field.preset"): preset_name or request.preset_id,
            tr("preflight.field.preset_id"): request.preset_id,
            tr("preflight.field.provider"): request.provider,
            tr("preflight.field.model"): f"{request.model_short_name}  ·  {request.model_id}",
            tr("preflight.field.size"): request.size,
            tr("preflight.field.ratio"): request.ratio,
            **(
                {
                    tr("preflight.field.source_format"): request.source_fmt.upper(),
                    tr("preflight.field.wire_source_format"): (
                        request.wire_source_fmt or request.source_fmt
                    ).upper(),
                    tr("preflight.field.source_dpi"): "×".join(
                        f"{value:g}" for value in request.source_dpi
                    )
                    if request.source_dpi
                    else tr("common.not_provided"),
                    tr("preflight.field.annotation_format"): tr(
                        "preflight.annotation_format.png"
                    ),
                }
                if isinstance(request, EditRequest)
                else {}
            ),
        }

    @staticmethod
    def _to_image(value: QImage | bytes | None) -> QImage:
        if isinstance(value, QImage):
            return value
        if isinstance(value, bytes):
            return QImage.fromData(value)
        return QImage()

    @staticmethod
    def _preview_card(title: str, image: QImage) -> QFrame:
        card = QFrame()
        card.setObjectName("previewCard")
        layout = QVBoxLayout(card)
        label = QLabel(title)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview = QLabel()
        preview.setMinimumSize(220, 150)
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if image.isNull():
            preview.setText(tr("common.unavailable"))
        else:
            preview.setPixmap(
                QPixmap.fromImage(image).scaled(
                    300,
                    190,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        layout.addWidget(label)
        layout.addWidget(preview, 1)
        return card


__all__ = ["PreflightDialog"]
