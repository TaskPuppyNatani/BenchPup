"""Shared modal behavior for explicit catalog editors."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..context import GuiApplicationContext

try:
    from ...engine.services import is_database_integrity_error
except ImportError:  # pragma: no cover - exercised by top-level test imports.
    from engine.services import is_database_integrity_error  # type: ignore[no-redef]


class CatalogEditorDialog(QDialog):
    """Own common save/Cancel safety while subclasses own entity fields."""

    def __init__(
        self,
        context: GuiApplicationContext,
        *,
        title: str,
        parent: QWidget | None = None,
        confirm_close: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.confirm_close = confirm_close or self._ask_confirm_close
        self._dirty = False
        self._saving = False
        self._saved = False
        self.saved_record: Any | None = None
        self._validation_fields: dict[str, QWidget] = {}

        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(560, 460)
        self.resize(760, 640)

        self.form_body = QWidget()
        self.form = QFormLayout(self.form_body)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self.form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        self.form.setHorizontalSpacing(14)
        self.form.setVerticalSpacing(10)

        scroll = QScrollArea()
        scroll.setObjectName("catalogEditorScroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form_body)

        self.validation_summary = QLabel()
        self.validation_summary.setObjectName("validationSummary")
        self.validation_summary.setWordWrap(True)
        self.validation_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.validation_summary.setVisible(False)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.setAccessibleName("Catalog editor actions")
        self.save_button = self.button_box.button(QDialogButtonBox.StandardButton.Save)
        self.cancel_button = self.button_box.button(QDialogButtonBox.StandardButton.Cancel)
        if self.save_button is not None:
            self.save_button.setAccessibleName("Save catalog record")
        if self.cancel_button is not None:
            self.cancel_button.setAccessibleName("Cancel catalog editor")
        self.button_box.accepted.connect(self._save)
        self.button_box.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.validation_summary)
        layout.addWidget(self.button_box)

    def register_field(self, name: str, widget: QWidget) -> None:
        self._validation_fields[name] = widget

    def mark_dirty(self, *_args: object) -> None:
        if not self._saved:
            self._dirty = True

    def build_draft(self) -> Any:
        raise NotImplementedError

    def save_draft(self, draft: Any) -> Any:
        raise NotImplementedError

    def _save(self) -> bool:
        if self._saving or self._saved:
            return False
        self._saving = True
        self.validation_summary.setVisible(False)
        if self.save_button is not None:
            self.save_button.setEnabled(False)
        try:
            self.saved_record = self.save_draft(self.build_draft())
            self._saved = True
            self._dirty = False
            self.accept()
            return True
        except Exception as error:
            if is_database_integrity_error(error):
                self._show_failure("A record with that name already exists. Nothing was changed.")
            elif isinstance(error, ValueError):
                self._show_failure(f"The record could not be saved: {error}", error)
            else:
                self.context.logger.error(
                    "Catalog editor save failed",
                    exc_info=(type(error), error, error.__traceback__),
                )
                self._show_failure("The record could not be saved. See logs/error.log for details.")
            return False
        finally:
            self._saving = False
            if not self._saved and self.save_button is not None:
                self.save_button.setEnabled(True)

    def _show_failure(self, message: str, error: BaseException | None = None) -> None:
        self.validation_summary.setText(message)
        self.validation_summary.setVisible(True)
        if error is not None:
            message_text = str(error)
            for name, widget in self._validation_fields.items():
                if name in message_text:
                    widget.setFocus()
                    break

    def _ask_confirm_close(self) -> bool:
        answer = QMessageBox.question(
            self,
            "Discard unsaved changes?",
            "This editor has unsaved changes. Discard them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def reject(self) -> None:
        if self._dirty and not self._saved and not self.confirm_close():
            return
        super().reject()

    def closeEvent(self, event: object) -> None:
        if self._dirty and not self._saved and not self.confirm_close():
            event.ignore()  # type: ignore[attr-defined]
            return
        event.accept()  # type: ignore[attr-defined]
        super().closeEvent(event)  # type: ignore[arg-type]


__all__ = ("CatalogEditorDialog",)
