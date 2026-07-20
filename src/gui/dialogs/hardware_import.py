"""Review-before-save hardware report import workflow."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QMessageBox,
)

from ..context import GuiApplicationContext
from .hardware_profile_editor import HardwareProfileFieldsEditor

try:
    from ...engine.domain import HardwareProfile, now
    from ...engine.hardware_importers import (
        HardwareImportDecodeError,
        HardwareImportReadError,
        HardwareProfileDraft,
        HardwareParseError,
        HardwareParserDetection,
        HardwareParserDetectionError,
        HardwareTextCandidate,
        HardwareTextRead,
        UnsupportedHardwareEncodingError,
        UnknownHardwareParserError,
        read_hardware_text,
    )
    from ...engine.services import (
        HardwareProfileConflict,
        HardwareProfileNameConflictError,
        is_database_integrity_error,
    )
    from ...engine.settings import HardwareImportPreferences
except ImportError:  # pragma: no cover - exercised by top-level test imports.
    from engine.domain import HardwareProfile, now  # type: ignore[no-redef]
    from engine.hardware_importers import (  # type: ignore[no-redef]
        HardwareImportDecodeError,
        HardwareImportReadError,
        HardwareProfileDraft,
        HardwareParseError,
        HardwareParserDetection,
        HardwareParserDetectionError,
        HardwareTextCandidate,
        HardwareTextRead,
        UnsupportedHardwareEncodingError,
        UnknownHardwareParserError,
        read_hardware_text,
    )
    from engine.services import (  # type: ignore[no-redef]
        HardwareProfileConflict,
        HardwareProfileNameConflictError,
        is_database_integrity_error,
    )
    from engine.settings import HardwareImportPreferences  # type: ignore[no-redef]


AUTO_PARSER = "auto"
PARSER_LABELS = {
    AUTO_PARSER: "Auto-detect",
    "MSInfo32": "MSInfo32",
    "DXDiag": "DXDiag",
    "lshw --short": "lshw --short",
}
CONFLICT_REASON_LABELS = {
    "name": "Name",
    "computer_name": "Computer name",
    "cpu_gpu": "CPU/GPU",
}


class HardwareImportDialog(QDialog):
    """Resizable modal dialog with zero writes before final save."""

    import_completed = Signal(int)

    def __init__(
        self,
        context: GuiApplicationContext,
        parent: QWidget | None = None,
        *,
        confirm_close: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.confirm_close = confirm_close or (lambda: True)
        self._source_path: Path | None = None
        self._read_result: HardwareTextRead | None = None
        self._selected_text_candidate: HardwareTextCandidate | None = None
        self._detection: HardwareParserDetection | None = None
        self._draft: HardwareProfileDraft | None = None
        self._intended_imported_at: str | None = None
        self._automatic_detection = False
        self._conflicts: tuple[HardwareProfileConflict, ...] = ()
        self._resolution: str | None = None
        self._saving = False
        self._saved = False
        self._dirty = False
        self._suppress_draft_change = False
        self._import_validation_invalid = False
        self.saved_profile: HardwareProfile | None = None
        self.fields_editor: HardwareProfileFieldsEditor | None = None

        self.setObjectName("hardwareImportDialog")
        self.setWindowTitle("Import Hardware Profile")
        self.setAccessibleName("Import Hardware Profile")
        self.setModal(True)
        self.setMinimumSize(760, 620)
        self.resize(980, 820)

        preferences = context.settings.get_hardware_import_preferences(
            context.hardware_importers.source_names()
        )
        self._last_source_directory = preferences.source_directory
        self._initial_parser_override = preferences.parser_override

        source_group = QGroupBox("Source")
        source_form = QFormLayout(source_group)
        source_row = QWidget()
        source_layout = QHBoxLayout(source_row)
        source_layout.setContentsMargins(0, 0, 0, 0)
        self.source_edit = QLineEdit()
        self.source_edit.setReadOnly(True)
        self.source_edit.setAccessibleName("Hardware report source file")
        self.source_edit.setPlaceholderText("Choose a hardware report file")
        self.browse_button = QPushButton("Browse…")
        self.browse_button.setAccessibleName("Browse for hardware report")
        self.browse_button.clicked.connect(self.browse_source)
        source_layout.addWidget(self.source_edit, 1)
        source_layout.addWidget(self.browse_button)
        source_form.addRow("Source file", source_row)

        self.parser_combo = QComboBox()
        self.parser_combo.setAccessibleName("Hardware parser selection")
        self.parser_combo.addItem(PARSER_LABELS[AUTO_PARSER], AUTO_PARSER)
        for source_name in context.hardware_importers.source_names():
            self.parser_combo.addItem(PARSER_LABELS.get(source_name, source_name), source_name)
        parser_index = self.parser_combo.findData(self._initial_parser_override)
        self.parser_combo.setCurrentIndex(parser_index if parser_index >= 0 else 0)
        self.parser_combo.currentIndexChanged.connect(self._parser_changed)
        source_form.addRow("Parser", self.parser_combo)

        self.parser_status = QLabel("Choose a hardware report file.")
        self.parser_status.setAccessibleName("Hardware parser status")
        self.parser_status.setWordWrap(True)
        self.parser_status.setTextFormat(Qt.TextFormat.PlainText)
        source_form.addRow("Detection", self.parser_status)

        self.preview_group = QGroupBox("Parsed preview")
        self.preview_form = QFormLayout(self.preview_group)
        self.preview_values: dict[str, QLabel] = {}
        for label, key in (
            ("Profile name", "name"),
            ("Computer name", "computer_name"),
            ("CPU", "cpu"),
            ("GPU", "gpu"),
            ("VRAM (GB)", "vram_gb"),
            ("RAM (GB)", "ram_gb"),
            ("Operating system", "operating_system"),
            ("Backend versions", "backend_versions"),
            ("Notes", "notes"),
        ):
            value = QLabel("Not recorded")
            value.setAccessibleName(f"Parsed {label}")
            value.setWordWrap(True)
            value.setTextFormat(Qt.TextFormat.PlainText)
            self.preview_values[key] = value
            self.preview_form.addRow(label, value)
        self.preview_group.setVisible(False)

        self.draft_group = QGroupBox("Editable profile draft")
        self.draft_layout = QVBoxLayout(self.draft_group)
        self.draft_hint = QLabel("Edit the parsed values before saving.")
        self.draft_hint.setWordWrap(True)
        self.draft_layout.addWidget(self.draft_hint)
        self.draft_group.setVisible(False)

        provenance_group = QGroupBox("Import provenance")
        provenance_form = QFormLayout(provenance_group)
        self.provenance_source = self._value_label()
        self.provenance_mode = self._value_label()
        self.provenance_filename = self._value_label()
        self.provenance_timestamp = self._value_label()
        provenance_form.addRow("Parser", self.provenance_source)
        provenance_form.addRow("Selection", self.provenance_mode)
        provenance_form.addRow("Source filename", self.provenance_filename)
        provenance_form.addRow("Intended import time", self.provenance_timestamp)

        self.conflict_group = QGroupBox("Existing profile conflicts")
        conflict_layout = QVBoxLayout(self.conflict_group)
        self.conflict_summary = QLabel()
        self.conflict_summary.setWordWrap(True)
        conflict_layout.addWidget(self.conflict_summary)
        self.conflict_combo = QComboBox()
        self.conflict_combo.setAccessibleName("Matching hardware profile")
        self.conflict_combo.currentIndexChanged.connect(self._update_conflict_reason)
        conflict_layout.addWidget(self.conflict_combo)
        self.conflict_reason = QLabel()
        self.conflict_reason.setWordWrap(True)
        conflict_layout.addWidget(self.conflict_reason)
        conflict_actions = QHBoxLayout()
        self.update_existing_button = QPushButton("Update Existing")
        self.update_existing_button.setAccessibleName("Update selected hardware profile")
        self.update_existing_button.clicked.connect(self._update_existing)
        self.create_new_button = QPushButton("Create New")
        self.create_new_button.setAccessibleName("Create new hardware profile")
        self.create_new_button.clicked.connect(self._choose_create_new)
        self.cancel_conflict_button = QPushButton("Cancel Resolution")
        self.cancel_conflict_button.setAccessibleName("Cancel hardware conflict resolution")
        self.cancel_conflict_button.clicked.connect(self._cancel_conflicts)
        conflict_actions.addWidget(self.update_existing_button)
        conflict_actions.addWidget(self.create_new_button)
        conflict_actions.addWidget(self.cancel_conflict_button)
        conflict_layout.addLayout(conflict_actions)
        self.conflict_group.setVisible(False)

        self.warning_label = QLabel()
        self.warning_label.setAccessibleName("Hardware import warning")
        self.warning_label.setWordWrap(True)
        self.warning_label.setVisible(False)
        self.warning_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label = QLabel()
        self.error_label.setAccessibleName("Hardware import error")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 18, 18, 18)
        body_layout.setSpacing(12)
        body_layout.addWidget(source_group)
        body_layout.addWidget(self.preview_group)
        body_layout.addWidget(self.draft_group)
        body_layout.addWidget(provenance_group)
        body_layout.addWidget(self.conflict_group)
        body_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setObjectName("hardwareImportScroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)

        self.save_button = QPushButton("Save Hardware Profile")
        self.save_button.setObjectName("primaryButton")
        self.save_button.setAccessibleName("Save hardware profile")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setAccessibleName("Cancel hardware import")
        self.cancel_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.warning_label)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)

    @staticmethod
    def _value_label() -> QLabel:
        label = QLabel("Not recorded")
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _display_value(value: object) -> str:
        if value is None or value == "":
            return "Not recorded"
        if isinstance(value, dict):
            return " | ".join(f"{key}={item}" for key, item in value.items()) or "Not recorded"
        return str(value)

    @staticmethod
    def _validated_directory(candidate: str | Path | None) -> Path | None:
        if candidate is None:
            return None
        try:
            resolved = Path(candidate).expanduser().resolve(strict=True)
            return resolved if resolved.is_dir() else None
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return None

    def _source_dialog_directory(self) -> Path | None:
        candidates: list[str | Path | None] = [
            self._last_source_directory,
            self.context.default_working_directory,
        ]
        try:
            candidates.append(Path.cwd())
        except (OSError, RuntimeError, ValueError):
            pass
        for candidate in candidates:
            directory = self._validated_directory(candidate)
            if directory is not None:
                return directory
        return None

    def _current_import_validation(self) -> tuple[bool, str]:
        if self._draft is None or self.fields_editor is None:
            return False, "Parse a hardware report before saving"
        try:
            self.context.catalog.validate_imported_hardware_profile(self._candidate_from_fields())
        except ValueError as error:
            return False, str(error)
        return True, ""

    def _refresh_save_eligibility(self) -> None:
        if self._saving or self._saved:
            self.save_button.setEnabled(False)
            return
        valid, message = self._current_import_validation()
        if not valid and self._draft is not None:
            self.warning_label.setText(f"{message.capitalize()}.")
            self.warning_label.setVisible(True)
            self._import_validation_invalid = True
        elif valid and self._import_validation_invalid:
            self.warning_label.clear()
            self.warning_label.setVisible(False)
            self._import_validation_invalid = False
        can_save = valid and self._resolution in {None, "create_new"} and (
            not self._conflicts or self._resolution == "create_new"
        )
        self.save_button.setEnabled(can_save)

    def browse_source(self) -> None:
        start = self._source_dialog_directory()
        if start is None:
            self._show_error("No accessible folder is available for choosing a hardware report.")
            return
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Hardware Report",
            str(start),
            "Hardware reports (*.txt *.log *.xml);;All files (*)",
        )
        if not selected:
            return
        self._source_path = Path(selected)
        self.source_edit.setText(str(self._source_path))
        self._clear_import_state()
        self._load_source()

    def _parser_changed(self, _index: int) -> None:
        """Clear all dependent state before parsing with the new selection."""

        self._clear_import_state()
        if self._source_path is not None:
            self._load_source()

    def _selected_parser(self) -> str:
        value = self.parser_combo.currentData()
        return str(value or AUTO_PARSER)

    def _clear_import_state(self) -> None:
        self._read_result = None
        self._selected_text_candidate = None
        self._detection = None
        self._draft = None
        self._intended_imported_at = None
        self._automatic_detection = False
        self._resolution = None
        self._conflicts = ()
        self._import_validation_invalid = False
        self._remove_fields_editor()
        self.preview_group.setVisible(False)
        self.draft_group.setVisible(False)
        for label in self.preview_values.values():
            label.setText("Not recorded")
        self.provenance_source.setText("Not recorded")
        self.provenance_mode.setText("Not recorded")
        self.provenance_filename.setText("Not recorded")
        self.provenance_timestamp.setText("Not recorded")
        self.conflict_group.setVisible(False)
        self.conflict_combo.clear()
        self.conflict_reason.clear()
        self.warning_label.clear()
        self.warning_label.setVisible(False)
        self.error_label.clear()
        self.error_label.setVisible(False)
        self.parser_status.setText("Loading hardware report…" if self._source_path else "Choose a hardware report file.")
        self.save_button.setEnabled(False)

    def _remove_fields_editor(self) -> None:
        if self.fields_editor is None:
            return
        self.draft_layout.removeWidget(self.fields_editor)
        self.fields_editor.deleteLater()
        self.fields_editor = None

    def _load_source(self) -> None:
        if self._source_path is None:
            return
        try:
            read_result = read_hardware_text(self._source_path)
        except UnsupportedHardwareEncodingError as error:
            self._show_error(str(error))
            return
        except HardwareImportDecodeError as error:
            self._show_error(str(error))
            return
        except HardwareImportReadError as error:
            self._show_error(str(error))
            return
        self._read_result = read_result
        selected_parser = self._selected_parser()
        if selected_parser == AUTO_PARSER:
            try:
                resolution = self.context.hardware_importers.resolve_decode_candidates(read_result.candidates)
            except HardwareParserDetectionError as error:
                self._show_error(str(error))
                return
            self._automatic_detection = True
        else:
            try:
                resolution = self.context.hardware_importers.resolve_parser_candidates(
                    selected_parser,
                    read_result.candidates,
                )
            except UnknownHardwareParserError as error:
                self._show_error(str(error))
                return
            self._automatic_detection = False

        if resolution.status != "matched" or resolution.selected is None:
            self.parser_status.setText(resolution.reason)
            return
        match = resolution.selected
        detection = match.detection
        parser_name = detection.parser_name
        if parser_name is None:
            self.parser_status.setText("The selected decoding did not identify a hardware parser.")
            return
        self._selected_text_candidate = match.candidate
        self._detection = detection

        try:
            draft = self.context.hardware_importers.parse_selected(parser_name, match.candidate.text)
        except HardwareParseError as error:
            self._show_error(f"{error}. Choose another parser or edit the source file.")
            return
        self._show_draft(draft, parser_name)

    def _show_draft(self, draft: HardwareProfileDraft, parser_name: str) -> None:
        self._draft = draft
        self._intended_imported_at = now()
        candidate = self.context.catalog.build_imported_hardware_profile(
            draft,
            imported_at=self._intended_imported_at,
        )
        self._replace_fields_editor(candidate)
        for key in self.preview_values:
            self.preview_values[key].setText(self._display_value(getattr(draft, key)))
        self.preview_group.setVisible(True)
        self.draft_group.setVisible(True)
        self.provenance_source.setText(parser_name)
        self.provenance_mode.setText("Automatically detected" if self._automatic_detection else "Manually selected")
        self.provenance_filename.setText(self._read_result.path.name if self._read_result else "Not recorded")
        self.provenance_timestamp.setText(self._intended_imported_at)
        self.parser_status.setText(f"{parser_name} parsed the hardware report successfully.")
        if not any(str(getattr(draft, key) or "").strip() for key in ("name", "computer_name", "cpu", "gpu", "operating_system")):
            self.warning_label.setText("The parser found no identifying hardware values. Review the draft before saving.")
            self.warning_label.setVisible(True)
        self._refresh_save_eligibility()
        self._dirty = True

    def _replace_fields_editor(self, candidate: HardwareProfile) -> None:
        self._remove_fields_editor()
        self.fields_editor = HardwareProfileFieldsEditor(candidate, self.draft_group)
        self.fields_editor.changed.connect(self._draft_changed)
        self.draft_layout.addWidget(self.fields_editor)

    def _draft_changed(self) -> None:
        if self._suppress_draft_change:
            return
        self._resolution = None
        self._conflicts = ()
        self.conflict_group.setVisible(False)
        self._refresh_save_eligibility()
        self._dirty = True

    def _update_preview_conflicts(self, conflicts: tuple[HardwareProfileConflict, ...]) -> None:
        self._conflicts = conflicts
        self.conflict_combo.clear()
        for conflict in conflicts:
            self.conflict_combo.addItem(conflict.profile.name, conflict.profile.id)
        self.conflict_combo.setCurrentIndex(-1)
        self.conflict_summary.setText(
            "Existing profiles match this imported hardware. Select a target to update, or choose Create New."
        )
        self.conflict_reason.clear()
        self.update_existing_button.setEnabled(False)
        self.conflict_group.setVisible(True)
        self.save_button.setEnabled(False)

    def _update_conflict_reason(self, index: int) -> None:
        if index < 0 or index >= len(self._conflicts):
            self.conflict_reason.clear()
            self.update_existing_button.setEnabled(False)
            return
        conflict = self._conflicts[index]
        reasons = ", ".join(CONFLICT_REASON_LABELS[reason] for reason in conflict.reasons)
        self.conflict_reason.setText(f"Matches: {reasons}")
        self.update_existing_button.setEnabled(not self._saving)

    def _cancel_conflicts(self) -> None:
        self._resolution = None
        self.conflict_group.setVisible(False)
        self._refresh_save_eligibility()

    def _choose_create_new(self) -> None:
        if self.fields_editor is None or self._draft is None:
            return
        candidate = self.context.catalog.build_imported_hardware_profile(
            self._draft,
            imported_at=self._intended_imported_at,
        )
        suggested = self.context.catalog.next_available_hardware_profile_name(candidate.name)
        self._suppress_draft_change = True
        try:
            self.fields_editor.name_edit.setText(suggested)
        finally:
            self._suppress_draft_change = False
        self._resolution = "create_new"
        self.conflict_summary.setText("Create New selected. Review the suggested name and save when ready.")
        self._refresh_save_eligibility()
        self._dirty = True

    def _update_existing(self) -> None:
        if self.conflict_combo.currentIndex() < 0:
            self._show_error("Select an existing hardware profile to update.")
            return
        self._resolution = "update"
        self._save()

    def _candidate_from_fields(self, existing: HardwareProfile | None = None) -> HardwareProfile:
        if self.fields_editor is None or self._draft is None:
            raise ValueError("Parse a hardware report before saving")
        candidate = self.context.catalog.build_imported_hardware_profile(
            self._draft,
            existing=existing,
            imported_at=self._intended_imported_at,
        )
        return self.fields_editor.build_profile(candidate)

    def _save(self) -> bool:
        if self._saving or self._saved:
            return False
        self._saving = True
        self.save_button.setEnabled(False)
        self.browse_button.setEnabled(False)
        self.parser_combo.setEnabled(False)
        self.update_existing_button.setEnabled(False)
        self.create_new_button.setEnabled(False)
        try:
            if self._resolution == "update":
                conflict = self._conflicts[self.conflict_combo.currentIndex()]
                existing = self.context.catalog.get_hardware_profile(conflict.profile.id) if conflict.profile.id is not None else None
                if existing is None:
                    raise ValueError("The selected hardware profile is no longer available. Refresh and try again.")
                candidate = self._candidate_from_fields(existing)
                self.context.catalog.validate_imported_hardware_profile(candidate)
                saved = self.context.catalog.update_hardware_profile(candidate)
            else:
                candidate = self._candidate_from_fields()
                self.context.catalog.validate_imported_hardware_profile(candidate)
                if self._resolution != "create_new":
                    conflicts = self.context.catalog.find_hardware_profile_conflicts(candidate)
                    if conflicts:
                        self._update_preview_conflicts(conflicts)
                        return False
                saved = self.context.catalog.create_imported_hardware_profile(candidate)
        except Exception as error:
            self._show_save_failure(error)
            return False
        finally:
            self._saving = False
            if not self._saved:
                self.browse_button.setEnabled(True)
                self.parser_combo.setEnabled(True)
                if self._resolution == "create_new":
                    self._refresh_save_eligibility()
                elif self._resolution is None:
                    self._refresh_save_eligibility()
                self.create_new_button.setEnabled(bool(self._conflicts) and self._resolution is None)
                self._update_conflict_reason(self.conflict_combo.currentIndex())

        self.saved_profile = saved
        self._saved = True
        self._dirty = False
        self._remember_preferences()
        if saved.id is not None:
            self.import_completed.emit(saved.id)
        self.accept()
        return True

    def _remember_preferences(self) -> None:
        if self._source_path is None:
            return
        try:
            self.context.settings.set_hardware_import_preferences(
                HardwareImportPreferences(
                    source_directory=self._source_path.parent,
                    parser_override=self._selected_parser(),
                )
            )
        except Exception:
            self.context.logger.exception("Hardware import settings could not be saved")

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(True)
        QMessageBox.warning(self, "Hardware import", message)

    def _show_save_failure(self, error: BaseException) -> None:
        if isinstance(error, HardwareProfileNameConflictError):
            message = "A hardware profile with that normalized name already exists. Nothing was changed."
        elif is_database_integrity_error(error):
            message = "A hardware profile with that name already exists. Nothing was changed."
        elif isinstance(error, ValueError):
            message = f"The hardware profile could not be saved: {error}"
        else:
            self.context.logger.error(
                "Hardware profile import save failed",
                exc_info=(type(error), error, error.__traceback__),
            )
            message = "The hardware profile could not be saved. See logs/error.log for details."
        self._show_error(message)

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


__all__ = ("HardwareImportDialog",)
