"""Dedicated Add/Edit dialog for RunAttachment metadata and file selection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QFileDialog,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QMessageBox,
    QPushButton,
    QWidget,
)

from ..context import GuiApplicationContext

try:
    from ...engine.domain import ATTACHMENT_TYPES, RunAttachment
    from ...engine.services import AttachmentStorageError
    from ...engine.settings import AttachmentPreferences
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
    from engine.domain import ATTACHMENT_TYPES, RunAttachment  # type: ignore[no-redef]
    from engine.services import AttachmentStorageError  # type: ignore[no-redef]
    from engine.settings import AttachmentPreferences  # type: ignore[no-redef]

from .base import CatalogEditorDialog


REFERENCE_MODE = "reference"
MANAGED_MODE = "managed"
STORAGE_MODE_LABELS = (
    ("Reference Original File", REFERENCE_MODE),
    ("Copy Into Managed Folder", MANAGED_MODE),
)
ATTACHMENT_TYPE_LABELS = {
    value: value.replace("_", " ").title()
    for value in ATTACHMENT_TYPES
}


class AttachmentEditorDialog(CatalogEditorDialog):
    """Create or edit one attachment without changing its file contents."""

    def __init__(
        self,
        context: GuiApplicationContext,
        run_id: int,
        attachment: RunAttachment | None = None,
        parent: QWidget | None = None,
        *,
        confirm_close: Callable[[], bool] | None = None,
    ) -> None:
        run, _, _ = context.benchmarks.get_run(run_id)
        if run is None:
            raise LookupError(f"Run #{run_id} was not found.")
        if attachment is not None and (attachment.id is None or attachment.run_id != run_id):
            raise ValueError("attachment does not belong to the selected run")

        self.run_id = run_id
        self.attachment = attachment
        self._preferences = context.settings.get_attachment_preferences()
        super().__init__(
            context,
            title=f"Edit Attachment - Run #{run_id}" if attachment is not None else f"Add Attachment - Run #{run_id}",
            parent=parent,
            confirm_close=confirm_close,
        )
        self.setObjectName("attachmentEditorDialog")
        self.setAccessibleName("Edit Attachment" if attachment is not None else "Add Attachment")
        if self.save_button is not None:
            self.save_button.setAccessibleName("Save attachment")
        if self.cancel_button is not None:
            self.cancel_button.setAccessibleName("Cancel attachment editor")

        self.attachment_type_combo = QComboBox()
        self.attachment_type_combo.setObjectName("attachmentType")
        self.attachment_type_combo.setAccessibleName("Attachment Type")
        for value in ATTACHMENT_TYPES:
            self.attachment_type_combo.addItem(ATTACHMENT_TYPE_LABELS[value], value)
        if attachment is not None:
            index = self.attachment_type_combo.findData(attachment.attachment_type)
            if index >= 0:
                self.attachment_type_combo.setCurrentIndex(index)
        self.register_field("attachment_type", self.attachment_type_combo)

        self.source_file_edit = QLineEdit(attachment.file_path if attachment is not None else "")
        self.source_file_edit.setObjectName("sourceFile")
        self.source_file_edit.setAccessibleName("Source File")
        self.source_file_edit.setPlaceholderText("Required")
        self.register_field("file_path", self.source_file_edit)
        source_browse = QPushButton("Browse")
        source_browse.setAccessibleName("Browse for source file")
        source_browse.clicked.connect(self._browse_source)
        source_row = QWidget()
        source_layout = QHBoxLayout(source_row)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(8)
        source_layout.addWidget(self.source_file_edit, 1)
        source_layout.addWidget(source_browse)

        self.storage_mode_combo = QComboBox()
        self.storage_mode_combo.setObjectName("storageMode")
        self.storage_mode_combo.setAccessibleName("Storage Mode")
        for label, value in STORAGE_MODE_LABELS:
            self.storage_mode_combo.addItem(label, value)
        if attachment is None:
            preferred_mode = self._preferences.storage_mode
            index = self.storage_mode_combo.findData(preferred_mode)
            self.storage_mode_combo.setCurrentIndex(index if index >= 0 else 0)
        else:
            # Storage mode is intentionally not persisted. Editing defaults to
            # reference mode so a metadata edit can never copy silently.
            self.storage_mode_combo.setCurrentIndex(self.storage_mode_combo.findData(REFERENCE_MODE))

        self.destination_folder_edit = QLineEdit(
            str(self._preferences.destination_directory) if self._preferences.destination_directory else ""
        )
        self.destination_folder_edit.setObjectName("destinationFolder")
        self.destination_folder_edit.setAccessibleName("Destination Folder")
        self.destination_folder_edit.setPlaceholderText("Required for managed copies")
        destination_browse = QPushButton("Browse")
        destination_browse.setAccessibleName("Browse for destination folder")
        destination_browse.clicked.connect(self._browse_destination)
        self._destination_browse = destination_browse
        destination_row = QWidget()
        destination_layout = QHBoxLayout(destination_row)
        destination_layout.setContentsMargins(0, 0, 0, 0)
        destination_layout.setSpacing(8)
        destination_layout.addWidget(self.destination_folder_edit, 1)
        destination_layout.addWidget(destination_browse)

        self.filename_edit = QLineEdit(
            attachment.original_filename
            if attachment is not None
            else self._filename_from_source(self.source_file_edit.text())
        )
        self.filename_edit.setObjectName("originalFilename")
        self.filename_edit.setAccessibleName("Filename")
        self.filename_edit.setPlaceholderText("Required")
        self.register_field("original_filename", self.filename_edit)

        self.notes_edit = QPlainTextEdit(attachment.notes if attachment is not None else "")
        self.notes_edit.setObjectName("attachmentNotes")
        self.notes_edit.setAccessibleName("Notes")
        self.notes_edit.setMinimumHeight(100)

        mode_hint = QLabel(
            "Editing uses reference mode by default. Select Copy Into Managed Folder explicitly to create a new managed copy."
            if attachment is not None
            else "Reference mode stores the selected path. Managed mode copies the source and preserves the original filename metadata."
        )
        mode_hint.setObjectName("fieldHint")
        mode_hint.setWordWrap(True)

        file_group = QGroupBox("File")
        file_form = QFormLayout(file_group)
        file_form.addRow("Source File *", source_row)
        file_form.addRow("Storage Mode", self.storage_mode_combo)
        file_form.addRow("Destination Folder", destination_row)
        file_form.addRow("Filename *", self.filename_edit)
        file_form.addRow("", mode_hint)

        text_group = QGroupBox("Attachment metadata")
        text_form = QFormLayout(text_group)
        text_form.addRow("Notes", self.notes_edit)

        self.form.addRow("Attachment Type *", self.attachment_type_combo)
        self.form.addRow(file_group)
        self.form.addRow(text_group)

        self.attachment_type_combo.currentIndexChanged.connect(self.mark_dirty)
        self.source_file_edit.textChanged.connect(self.mark_dirty)
        self.storage_mode_combo.currentIndexChanged.connect(self._storage_mode_changed)
        self.destination_folder_edit.textChanged.connect(self.mark_dirty)
        self.filename_edit.textChanged.connect(self.mark_dirty)
        self.notes_edit.textChanged.connect(self.mark_dirty)
        self._refresh_storage_controls()

    @staticmethod
    def _filename_from_source(source_path: str) -> str:
        if not source_path:
            return ""
        try:
            return Path(source_path).name
        except (OSError, ValueError):
            return ""

    def _storage_mode_changed(self, *_args: object) -> None:
        self._refresh_storage_controls()
        self.mark_dirty()

    def _refresh_storage_controls(self) -> None:
        managed = self.storage_mode_combo.currentData() == MANAGED_MODE
        self.destination_folder_edit.setEnabled(managed)
        self._destination_browse.setEnabled(managed)

    def _browse_source(self) -> None:
        current = self.source_file_edit.text()
        if not current and self._preferences.source_directory:
            current = str(self._preferences.source_directory)
        selected, _ = QFileDialog.getOpenFileName(self, "Select attachment file", current)
        if not selected:
            return
        self.source_file_edit.setText(selected)
        if self.attachment is None:
            self.filename_edit.setText(self._filename_from_source(selected))

    def _browse_destination(self) -> None:
        current = self.destination_folder_edit.text()
        if not current and self._preferences.destination_directory:
            current = str(self._preferences.destination_directory)
        selected = QFileDialog.getExistingDirectory(self, "Select managed destination folder", current)
        if selected:
            self.destination_folder_edit.setText(selected)

    def build_draft(self) -> RunAttachment:
        values: dict[str, Any] = {
            "run_id": self.run_id,
            "attachment_type": str(self.attachment_type_combo.currentData() or ""),
            "file_path": self.source_file_edit.text().strip(),
            "original_filename": self.filename_edit.text().strip(),
            "notes": self.notes_edit.toPlainText(),
        }
        if self.attachment is not None:
            return replace(self.attachment, **values)
        return RunAttachment(**values)

    def save_draft(self, draft: RunAttachment) -> RunAttachment:
        mode = str(self.storage_mode_combo.currentData() or REFERENCE_MODE)
        source_path = draft.file_path
        managed_destination: str | None = None

        if self.attachment is not None and mode == REFERENCE_MODE and draft.file_path == self.attachment.file_path:
            # Metadata-only edits preserve even a missing or inaccessible path.
            source_path = None
        elif mode == MANAGED_MODE:
            managed_destination = self.destination_folder_edit.text().strip()
            if not managed_destination:
                raise AttachmentStorageError("Choose a destination folder for managed storage.")
        elif mode != REFERENCE_MODE:
            raise AttachmentStorageError("Choose a valid attachment storage mode.")

        saved = self.context.benchmarks.save_attachment_from_source(
            draft,
            source_path=source_path,
            managed_destination=managed_destination,
        )
        if self.attachment is None:
            self._remember_preferences(draft.file_path, managed_destination, mode)
        return saved

    def _remember_preferences(self, source_path: str, destination: str | None, mode: str) -> None:
        source_directory: Path | None = self._preferences.source_directory
        if source_path:
            try:
                source_directory = Path(source_path).expanduser().resolve(strict=False).parent
            except (OSError, RuntimeError, ValueError):
                pass
        destination_directory = self._preferences.destination_directory
        if destination:
            try:
                destination_directory = Path(destination).expanduser().resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                pass
        try:
            self.context.settings.set_attachment_preferences(
                AttachmentPreferences(
                    source_directory=source_directory,
                    destination_directory=destination_directory,
                    storage_mode=mode,
                )
            )
        except (OSError, ValueError):
            self.context.logger.exception("Could not remember attachment dialog preferences")

    def _show_failure(self, message: str, error: BaseException | None = None) -> None:
        super()._show_failure(message, error)
        if isinstance(error, AttachmentStorageError):
            QMessageBox.warning(self, "Attachment could not be saved", str(error))


__all__ = (
    "ATTACHMENT_TYPE_LABELS",
    "AttachmentEditorDialog",
    "MANAGED_MODE",
    "REFERENCE_MODE",
    "STORAGE_MODE_LABELS",
)
