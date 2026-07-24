"""Modal backup and restore workflow for the BenchPup desktop shell."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

try:  # Support both ``python -m src.gui`` and test imports with ``src`` on PATH.
    from ...engine.archive import (
        ArchiveError,
        ArchiveExportResult,
        ArchiveFingerprint,
        ArchiveIssue,
        ArchiveIssueSeverity,
        ArchiveMergeResult,
        ArchivePreview,
        ArchiveReplaceResult,
    )
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
    from engine.archive import (  # type: ignore[no-redef]
        ArchiveError,
        ArchiveExportResult,
        ArchiveFingerprint,
        ArchiveIssue,
        ArchiveIssueSeverity,
        ArchiveMergeResult,
        ArchivePreview,
        ArchiveReplaceResult,
    )
from ..context import GuiApplicationContext


ATTACHMENT_WORDING = (
    "Attachment records and stored paths are included. External attachment file contents are not included in the archive."
)
ARCHIVE_FILE_FILTER = "BenchPup archives (*.json)"
DEFAULT_BACKUP_FILENAME = "benchpup-backup.json"


class BackupRestoreDialog(QDialog):
    """Configure an archive backup or restore one through typed engine APIs."""

    backup_succeeded = Signal(str)
    restore_succeeded = Signal(str)

    def __init__(
        self,
        context: GuiApplicationContext,
        parent: QWidget | None = None,
        *,
        confirm_backup: Callable[[Path], bool] | None = None,
        confirm_overwrite: Callable[[], bool] | None = None,
        confirm_merge: Callable[[], bool] | None = None,
        confirm_replace: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self._confirm_backup = confirm_backup
        self._confirm_overwrite = confirm_overwrite
        self._confirm_merge = confirm_merge
        self._confirm_replace = confirm_replace
        self._initializing = True
        self._busy = False
        self._preview: ArchivePreview | None = None
        self._selected_archive_path: Path | None = None
        self._archive_fingerprint: ArchiveFingerprint | None = None
        self._restore_completed = False
        self._last_backup_path: Path | None = None
        self._initial_directory = self._first_valid_directory(
            context.default_working_directory,
            context.paths.project_root,
            self._safe_cwd(),
        )

        self.setObjectName("backupRestoreDialog")
        self.setAccessibleName("Backup and Restore dialog")
        self.setWindowTitle("Backup & Restore")
        self.setModal(True)
        self.setMinimumSize(820, 720)
        self.resize(980, 860)
        self._build_ui()
        self._set_initial_destination()
        self._initializing = False
        self._update_controls()

    def _build_ui(self) -> None:
        title = QLabel("Backup & Restore")
        title.setObjectName("dialogTitle")
        description = QLabel(
            "Create an atomic BenchPup archive, or preview and restore a validated archive into the current database."
        )
        description.setObjectName("dialogDescription")
        description.setWordWrap(True)

        self.backup_destination_edit = QLineEdit()
        self.backup_destination_edit.setObjectName("backupDestinationEdit")
        self.backup_destination_edit.setAccessibleName("Backup destination")
        self.backup_destination_edit.setPlaceholderText("Choose an archive destination")
        self.backup_destination_edit.textChanged.connect(self._backup_destination_changed)
        self.backup_browse_button = QPushButton("Browse…")
        self.backup_browse_button.setObjectName("backupBrowseButton")
        self.backup_browse_button.setAccessibleName("Browse for backup destination")
        self.backup_browse_button.clicked.connect(self.browse_backup_destination)
        backup_destination_row = QHBoxLayout()
        backup_destination_row.addWidget(self.backup_destination_edit, 1)
        backup_destination_row.addWidget(self.backup_browse_button)

        self.backup_button = QPushButton("Create Backup")
        self.backup_button.setObjectName("backupButton")
        self.backup_button.setAccessibleName("Create BenchPup backup")
        self.backup_button.clicked.connect(self.create_backup)
        self.backup_result_label = QLabel()
        self.backup_result_label.setObjectName("backupResult")
        self.backup_result_label.setWordWrap(True)
        self.backup_result_label.setVisible(False)

        backup_group = QGroupBox("Create Backup")
        backup_form = QFormLayout(backup_group)
        backup_form.addRow("Destination", backup_destination_row)
        backup_form.addRow(self.backup_button)
        backup_form.addRow(self.backup_result_label)
        backup_attachment_note = QLabel(ATTACHMENT_WORDING)
        backup_attachment_note.setWordWrap(True)
        backup_form.addRow(backup_attachment_note)

        self.restore_archive_edit = QLineEdit()
        self.restore_archive_edit.setObjectName("restoreArchiveEdit")
        self.restore_archive_edit.setAccessibleName("Archive to restore")
        self.restore_archive_edit.setPlaceholderText("Choose an existing BenchPup archive")
        self.restore_archive_edit.textChanged.connect(self._archive_path_changed)
        self.restore_browse_button = QPushButton("Browse…")
        self.restore_browse_button.setObjectName("restoreBrowseButton")
        self.restore_browse_button.setAccessibleName("Browse for archive to restore")
        self.restore_browse_button.clicked.connect(self.browse_restore_archive)
        restore_archive_row = QHBoxLayout()
        restore_archive_row.addWidget(self.restore_archive_edit, 1)
        restore_archive_row.addWidget(self.restore_browse_button)

        self.preview_button = QPushButton("Preview Archive")
        self.preview_button.setObjectName("archivePreviewButton")
        self.preview_button.setAccessibleName("Preview selected archive")
        self.preview_button.clicked.connect(self.preview_archive)

        self.preview_summary = QLabel("No archive preview yet.")
        self.preview_summary.setObjectName("archivePreviewSummary")
        self.preview_summary.setWordWrap(True)

        self.archive_details_label = QLabel()
        self.archive_details_label.setObjectName("archiveDetails")
        self.archive_details_label.setWordWrap(True)

        self.warning_label = QLabel("Warnings: None")
        self.warning_label.setObjectName("archiveWarnings")
        self.warning_label.setWordWrap(True)

        self.error_label = QLabel("Errors: None")
        self.error_label.setObjectName("archiveErrors")
        self.error_label.setWordWrap(True)

        self.merge_button = QPushButton("Merge Into Current Database")
        self.merge_button.setObjectName("mergeArchiveButton")
        self.merge_button.setAccessibleName("Merge archive into current database")
        self.merge_button.clicked.connect(self.merge_restore)
        self.replace_button = QPushButton("Replace Current Database")
        self.replace_button.setObjectName("replaceArchiveButton")
        self.replace_button.setAccessibleName("Replace current database from archive")
        self.replace_button.clicked.connect(self.replace_restore)

        restore_actions = QHBoxLayout()
        restore_actions.addWidget(self.preview_button)
        restore_actions.addStretch(1)
        restore_actions.addWidget(self.merge_button)
        restore_actions.addWidget(self.replace_button)

        self.restore_result_label = QLabel()
        self.restore_result_label.setObjectName("restoreResult")
        self.restore_result_label.setWordWrap(True)
        self.restore_result_label.setVisible(False)

        restore_group = QGroupBox("Restore from Archive")
        restore_layout = QVBoxLayout(restore_group)
        restore_form = QFormLayout()
        restore_form.addRow("Archive", restore_archive_row)
        restore_layout.addLayout(restore_form)
        restore_layout.addLayout(restore_actions)
        restore_layout.addWidget(self.preview_summary)
        restore_layout.addWidget(self.archive_details_label)
        restore_layout.addWidget(self.warning_label)
        restore_layout.addWidget(self.error_label)
        restore_layout.addWidget(self.restore_result_label)

        self.status_label = QLabel("Choose a backup destination or an archive to restore.")
        self.status_label.setObjectName("backupRestoreStatus")
        self.status_label.setWordWrap(True)
        self.open_file_button = QPushButton("Open File")
        self.open_file_button.setObjectName("backupOpenFileButton")
        self.open_file_button.setAccessibleName("Open created backup file")
        self.open_file_button.clicked.connect(self.open_file)
        self.open_folder_button = QPushButton("Open Containing Folder")
        self.open_folder_button.setObjectName("backupOpenFolderButton")
        self.open_folder_button.setAccessibleName("Open backup containing folder")
        self.open_folder_button.clicked.connect(self.open_folder)
        self.open_file_button.setVisible(False)
        self.open_folder_button.setVisible(False)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("backupRestoreCancelButton")
        self.cancel_button.setAccessibleName("Cancel backup and restore")
        self.cancel_button.clicked.connect(self.reject)

        actions = QHBoxLayout()
        actions.addWidget(self.status_label, 1)
        actions.addWidget(self.open_file_button)
        actions.addWidget(self.open_folder_button)
        actions.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addWidget(backup_group)
        layout.addWidget(restore_group, 1)
        layout.addLayout(actions)

    def _set_initial_destination(self) -> None:
        if self._initial_directory is None:
            self.status_label.setText(
                "No valid working folder is available. Choose a valid destination folder to continue."
            )
            self.backup_destination_edit.clear()
            return
        self.backup_destination_edit.setText(str(self._initial_directory / DEFAULT_BACKUP_FILENAME))

    @staticmethod
    def _safe_cwd() -> Path | None:
        try:
            return Path.cwd()
        except OSError:
            return None

    @staticmethod
    def _first_valid_directory(*candidates: Path | None) -> Path | None:
        seen: set[Path] = set()
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                path = candidate.expanduser().resolve(strict=False)
                if path in seen:
                    continue
                seen.add(path)
                if path.exists() and path.is_dir():
                    return path
            except (OSError, RuntimeError, ValueError):
                continue
        return None

    @staticmethod
    def _normalise_path(value: str) -> Path | None:
        text = value.strip()
        if not text:
            return None
        try:
            return Path(text).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

    def _backup_destination_changed(self, _text: str) -> None:
        if not self._initializing:
            self.backup_result_label.clear()
            self.backup_result_label.setVisible(False)
            self.open_file_button.setVisible(False)
            self.open_folder_button.setVisible(False)
            self._last_backup_path = None
            self._update_controls()

    def _archive_path_changed(self, _text: str) -> None:
        if self._initializing:
            return
        self._clear_preview()
        self.status_label.setText("Archive selection changed. Preview again before restoring.")
        self._update_controls()

    def _clear_preview(self) -> None:
        self._preview = None
        self._selected_archive_path = None
        self._archive_fingerprint = None
        self._restore_completed = False
        self.preview_summary.setText("No archive preview yet.")
        self.archive_details_label.clear()
        self.warning_label.setText("Warnings: None")
        self.error_label.setText("Errors: None")
        self.restore_result_label.clear()
        self.restore_result_label.setVisible(False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.backup_destination_edit.setEnabled(not busy)
        self.backup_browse_button.setEnabled(not busy and self._initial_directory is not None)
        self.restore_archive_edit.setEnabled(not busy)
        self.restore_browse_button.setEnabled(not busy and self._initial_directory is not None)
        self.cancel_button.setEnabled(not busy)
        self._update_controls()

    def _update_controls(self) -> None:
        has_backup_destination = bool(self.backup_destination_edit.text().strip())
        self.backup_button.setEnabled(not self._busy and has_backup_destination)
        if self._busy:
            self.preview_button.setEnabled(False)
            self.merge_button.setEnabled(False)
            self.replace_button.setEnabled(False)
            return
        has_archive = bool(self.restore_archive_edit.text().strip())
        self.preview_button.setEnabled(has_archive)
        can_restore = (
            self._preview is not None
            and self._preview.valid
            and self._archive_fingerprint is not None
            and not self._restore_completed
        )
        self.merge_button.setEnabled(bool(can_restore and self._preview and self._preview.merge_eligible))
        self.replace_button.setEnabled(bool(can_restore and self._preview and self._preview.replace_eligible))

    def browse_backup_destination(self) -> None:
        if self._initial_directory is None:
            self.status_label.setText("No valid destination folder is available.")
            return
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Choose backup destination",
            str(self._initial_directory / DEFAULT_BACKUP_FILENAME),
            ARCHIVE_FILE_FILTER,
        )
        if selected:
            path = self._normalise_path(selected)
            if path is not None:
                self._initial_directory = self._first_valid_directory(path.parent)
            self.backup_destination_edit.setText(selected)

    def browse_restore_archive(self) -> None:
        if self._initial_directory is None:
            self.status_label.setText("No valid archive folder is available.")
            return
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose BenchPup archive",
            str(self._initial_directory),
            ARCHIVE_FILE_FILTER,
        )
        if selected:
            path = self._normalise_path(selected)
            if path is not None:
                self._initial_directory = self._first_valid_directory(path.parent)
            self.restore_archive_edit.setText(selected)

    def create_backup(self) -> None:
        if self._busy:
            return
        destination = self._normalise_path(self.backup_destination_edit.text())
        if destination is None:
            self.status_label.setText("Choose a valid backup destination before creating a backup.")
            return
        try:
            exists = destination.exists() or destination.is_symlink()
            is_directory = exists and destination.is_dir()
        except OSError:
            exists = True
            is_directory = False
        if is_directory:
            self.status_label.setText("The selected backup destination is a directory, not a file.")
            return
        if not self._ask_backup(destination, overwriting=exists):
            self.status_label.setText("Backup cancelled. No file or settings were changed.")
            return

        result: ArchiveExportResult | None = None
        self._set_busy(True)
        try:
            try:
                result = self.context.archives.export_typed(
                    destination,
                    self.context.version,
                    create_parent=False,
                )
            except ArchiveError as error:
                self._show_archive_error("Backup", error)
            except Exception:
                self.context.logger.exception("Backup export failed")
                self.status_label.setText("The backup could not be completed. Correct the destination and try again.")
        finally:
            self._set_busy(False)
        if result is not None:
            self._show_backup_result(result)
            self.backup_succeeded.emit(str(result.path))

    def _ask_backup(self, path: Path, *, overwriting: bool) -> bool:
        if overwriting and self._confirm_overwrite is not None:
            return bool(self._confirm_overwrite())
        if self._confirm_backup is not None:
            return bool(self._confirm_backup(path))
        if overwriting:
            answer = QMessageBox.question(
                self,
                "Confirm backup overwrite",
                f"The backup file already exists:\n{path}\n\nReplace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        else:
            answer = QMessageBox.question(
                self,
                "Confirm backup",
                f"Create an atomic BenchPup backup at:\n{path}\n\n{ATTACHMENT_WORDING}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        return answer == QMessageBox.StandardButton.Yes

    def _show_backup_result(self, result: ArchiveExportResult) -> None:
        self._last_backup_path = result.path
        lines = [f"Backup completed successfully:\n{result.path}", self._format_counts(result.counts)]
        warnings = self._format_issue_group(result.warnings, "Warnings")
        if warnings:
            lines.append(warnings)
        self.backup_result_label.setText("\n\n".join(lines))
        self.backup_result_label.setVisible(True)
        self.open_file_button.setVisible(True)
        self.open_folder_button.setVisible(True)
        self.status_label.setText("Backup complete.")

    def preview_archive(self) -> None:
        if self._busy:
            return
        archive_path = self._normalise_path(self.restore_archive_edit.text())
        self._clear_preview()
        if archive_path is None:
            self.status_label.setText("Choose an archive before previewing.")
            self._update_controls()
            return

        preview: ArchivePreview | None = None
        self._set_busy(True)
        try:
            try:
                preview = self.context.archives.preview_typed(archive_path)
            except ArchiveError as error:
                self._show_archive_error("Archive preview", error)
            except Exception:
                self.context.logger.exception("Archive preview failed")
                self.status_label.setText("The archive could not be previewed. Choose another archive and try again.")
        finally:
            self._set_busy(False)
        if preview is None:
            self._update_controls()
            return
        self._preview = preview
        self._selected_archive_path = preview.archive_path or archive_path
        self._archive_fingerprint = preview.fingerprint
        self._render_preview(preview)
        self._update_controls()

    def _render_preview(self, preview: ArchivePreview) -> None:
        state = "Valid" if preview.valid else "Invalid"
        self.preview_summary.setText(
            f"{state} archive preview. Compatibility: {preview.compatibility_state.value}. "
            f"Merge eligible: {'Yes' if preview.merge_eligible else 'No'}. "
            f"Replace eligible: {'Yes' if preview.replace_eligible else 'No'}."
        )
        declared = self._format_counts(preview.declared_counts, allow_none=True)
        actual = self._format_counts(preview.actual_counts)
        self.archive_details_label.setText(
            "\n".join(
                (
                    f"Archive format: {preview.format_identifier or 'Not recorded'}",
                    f"Archive version: {self._display_value(preview.archive_version)}",
                    f"BenchPup version: {preview.benchpup_version or 'Not recorded'}",
                    f"Archive schema version: {self._display_value(preview.archive_schema_version)}",
                    f"Current schema version: {preview.current_schema_version}",
                    f"Created at: {preview.created_at or 'Not recorded'}",
                    f"Declared counts: {declared}",
                    f"Actual counts: {actual}",
                    f"Total records: {preview.total_record_count}",
                    f"Attachment metadata records: {preview.attachment_metadata_count}",
                    ATTACHMENT_WORDING,
                )
            )
        )
        self.warning_label.setText(self._format_issue_group(preview.issues, "Warnings", ArchiveIssueSeverity.WARNING))
        self.error_label.setText(self._format_issue_group(preview.issues, "Errors", ArchiveIssueSeverity.ERROR))
        self.status_label.setText(
            "Preview ready. Choose Merge Into Current Database or Replace Current Database."
            if preview.valid
            else "Preview completed with errors. Correct the archive or choose another file."
        )

    def merge_restore(self) -> None:
        if not self._can_restore(merge=True) or self._busy:
            return
        archive_path = self._selected_archive_path
        fingerprint = self._archive_fingerprint
        if archive_path is None or fingerprint is None:
            return
        if not self._ask_merge():
            self.status_label.setText("Merge cancelled. No database or settings were changed.")
            return
        result: ArchiveMergeResult | None = None
        self._set_busy(True)
        try:
            try:
                result = self.context.archives.merge_file(
                    archive_path,
                    fingerprint=fingerprint,
                )
            except ArchiveError as error:
                self._show_archive_error("Merge", error)
            except Exception:
                self.context.logger.exception("Archive merge failed")
                self._show_restore_generic_error("Merge")
        finally:
            self._set_busy(False)
        if result is not None:
            self._show_merge_result(result)
            self.restore_succeeded.emit("merge")

    def _can_restore(self, *, merge: bool) -> bool:
        if (
            self._preview is None
            or not self._preview.valid
            or self._archive_fingerprint is None
            or self._selected_archive_path is None
            or self._restore_completed
        ):
            return False
        return self._preview.merge_eligible if merge else self._preview.replace_eligible

    def _ask_merge(self) -> bool:
        if self._confirm_merge is not None:
            return bool(self._confirm_merge())
        answer = QMessageBox.question(
            self,
            "Merge Into Current Database",
            "The current database remains in place. Compatible records will be added; duplicates may be skipped, "
            "IDs may be remapped, and the operation is transactional. No normal in-app undo exists. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _show_merge_result(self, result: ArchiveMergeResult) -> None:
        self._restore_completed = True
        lines = ["Merge completed successfully.", self._format_report(result.report)]
        warnings = self._format_issue_group(result.warnings, "Warnings")
        if warnings:
            lines.append(warnings)
        self.restore_result_label.setText("\n\n".join(lines))
        self.restore_result_label.setVisible(True)
        self.status_label.setText("Archive merge complete.")
        self._update_controls()

    def replace_restore(self) -> None:
        if not self._can_restore(merge=False) or self._busy:
            return
        archive_path = self._selected_archive_path
        fingerprint = self._archive_fingerprint
        if archive_path is None or fingerprint is None:
            return
        if not self._ask_replace():
            self.status_label.setText("Replace cancelled. No database or settings were changed.")
            return
        result: ArchiveReplaceResult | None = None
        self._set_busy(True)
        try:
            try:
                result = self.context.archives.replace_file(
                    archive_path,
                    self.context.version,
                    fingerprint=fingerprint,
                )
            except ArchiveError as error:
                self._show_archive_error("Replace", error)
            except Exception:
                self.context.logger.exception("Archive replacement failed")
                self._show_restore_generic_error("Replace")
        finally:
            self._set_busy(False)
        if result is not None:
            self._show_replace_result(result)
            self.restore_succeeded.emit("replace")

    def _ask_replace(self) -> bool:
        if self._confirm_replace is not None:
            return bool(self._confirm_replace())
        message = (
            "The active database will be replaced. Data missing from the backup will no longer exist in the active "
            "database. A safety backup is created first; validation, recovery, and rollback are owned by the archive "
            "engine. A restart may be required.\n\nContinue with this destructive operation?"
        )
        box = QMessageBox(QMessageBox.Icon.Warning, "Replace Current Database", message, parent=self)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        acknowledgement = QCheckBox("I understand this replaces the active database")
        acknowledgement.setCheckable(True)
        acknowledgement.setChecked(False)
        acknowledgement.setObjectName("replaceAcknowledgement")
        box.setCheckBox(acknowledgement)
        yes_button = box.button(QMessageBox.StandardButton.Yes)
        if yes_button is not None:
            yes_button.setEnabled(False)
        acknowledgement.toggled.connect(lambda checked: yes_button.setEnabled(checked) if yes_button else None)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _show_replace_result(self, result: ArchiveReplaceResult) -> None:
        self._restore_completed = True
        lines = [
            "Replace completed successfully.",
            self._format_report(result.report),
            f"Safety backup: {result.safety_backup_path}",
            f"Recovery status: {result.recovery_status.value}",
            f"Recovery path: {result.recovery_path or 'Not recorded'}",
            f"Restart required: {'Yes' if result.restart_required else 'No'}",
        ]
        warnings = self._format_issue_group(result.warnings, "Warnings")
        if warnings:
            lines.append(warnings)
        self.restore_result_label.setText("\n\n".join(lines))
        self.restore_result_label.setVisible(True)
        self.status_label.setText("Archive replacement complete.")
        self._update_controls()

    def _show_archive_error(self, action: str, error: ArchiveError) -> None:
        code = str(getattr(error.code, "value", error.code))
        detail = error.issue.message if error.issue is not None else str(error)
        self.status_label.setText(f"{action} failed [{code}]: {detail}")
        self.error_label.setText(f"Errors:\n- {detail}")
        if action == "Replace" and (
            error.safety_backup_path is not None or error.recovery_path is not None
        ):
            details = [
                f"Safety backup: {error.safety_backup_path or 'Not recorded'}",
                f"Recovery status: {error.recovery_status.value}",
                f"Recovery path: {error.recovery_path or 'Not recorded'}",
            ]
            self.restore_result_label.setText("\n".join(details))
            self.restore_result_label.setVisible(True)
        if action in {"Merge", "Replace"}:
            self._clear_restore_eligibility()

    def _show_restore_generic_error(self, action: str) -> None:
        self.status_label.setText(
            f"{action} failed. No database or settings were changed. Preview the archive again before retrying."
        )
        self.error_label.setText("Errors: The operation failed. See logs/error.log for details.")
        self._clear_restore_eligibility()

    def _clear_restore_eligibility(self) -> None:
        self._preview = None
        self._selected_archive_path = None
        self._archive_fingerprint = None
        self._restore_completed = False
        self.preview_summary.setText("Preview cleared. Preview the archive again before restoring.")
        self.archive_details_label.clear()
        self.warning_label.setText("Warnings: None")
        self.preview_button.setEnabled(bool(self.restore_archive_edit.text().strip()) and not self._busy)
        self.merge_button.setEnabled(False)
        self.replace_button.setEnabled(False)

    def open_file(self) -> None:
        if self._last_backup_path is None:
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_backup_path))):
            QMessageBox.warning(self, "Open file", "The backup succeeded, but the archive file could not be opened.")

    def open_folder(self) -> None:
        if self._last_backup_path is None:
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_backup_path.parent))):
            QMessageBox.warning(
                self,
                "Open containing folder",
                "The backup succeeded, but the containing folder could not be opened.",
            )

    @staticmethod
    def _display_value(value: object) -> str:
        return "Not recorded" if value is None else str(value)

    @staticmethod
    def _format_counts(counts: Mapping[str, object], *, allow_none: bool = False) -> str:
        if not counts:
            return "None"
        values: list[str] = []
        for name, count in counts.items():
            if count is None and not allow_none:
                continue
            values.append(f"{name}={count if count is not None else 'Not recorded'}")
        return ", ".join(values) or "None"

    @staticmethod
    def _format_issue_group(
        issues: tuple[ArchiveIssue, ...],
        title: str,
        severity: ArchiveIssueSeverity | None = None,
    ) -> str:
        selected = tuple(issue for issue in issues if severity is None or issue.severity is severity)
        if not selected:
            return f"{title}: None"
        lines = [f"{title}:"]
        for issue in selected:
            location = ", ".join(
                value
                for value in (issue.section, issue.table, issue.field)
                if value
            )
            suffix = f" ({location})" if location else ""
            lines.append(f"- [{issue.code.value}] {issue.message}{suffix}")
        return "\n".join(lines)

    @staticmethod
    def _format_report(report: object) -> str:
        lines = ["Restore result:"]
        any_values = False
        for label, attribute in (
            ("Created", "created"),
            ("Skipped", "skipped"),
            ("Updated", "updated"),
            ("Remapped", "remapped"),
            ("Failed", "failed"),
        ):
            values = getattr(report, attribute, {})
            nonzero = [f"{name}={count}" for name, count in values.items() if count]
            if nonzero:
                any_values = True
                lines.append(f"{label}: {', '.join(nonzero)}")
        if not any_values:
            lines.append("No record changes.")
        return "\n".join(lines)

    def reject(self) -> None:
        if self._busy:
            return
        super().reject()


__all__ = ("BackupRestoreDialog",)
