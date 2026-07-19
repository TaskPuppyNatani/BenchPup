"""Explicit Add/Edit dialog for BenchmarkSession records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone

from PySide6.QtCore import QDateTime, QTime, QTimeZone
from PySide6.QtWidgets import QCheckBox, QDateTimeEdit, QHBoxLayout, QLineEdit, QPlainTextEdit, QWidget

from ..context import GuiApplicationContext

try:
    from ...engine.domain import BenchmarkSession, serialize_utc_timestamp
    from ...engine.statistics import parse_utc_timestamp
except ImportError:  # pragma: no cover - exercised by top-level test imports.
    from engine.domain import BenchmarkSession, serialize_utc_timestamp  # type: ignore[no-redef]
    from engine.statistics import parse_utc_timestamp  # type: ignore[no-redef]

from .base import CatalogEditorDialog


class SessionEditorDialog(CatalogEditorDialog):
    DATE_TIME_DISPLAY_FORMAT = "yyyy-MM-dd HH:mm"

    def __init__(
        self,
        context: GuiApplicationContext,
        session: BenchmarkSession | None = None,
        parent: QWidget | None = None,
        *,
        confirm_close: Callable[[], bool] | None = None,
    ) -> None:
        self.session = session
        super().__init__(
            context,
            title="Edit Session" if session is not None else "Add Session",
            parent=parent,
            confirm_close=confirm_close,
        )
        self.setAccessibleName("Edit Session" if session is not None else "Add Session")
        self.system_timezone = QTimeZone.systemTimeZone()
        current_local = QDateTime.currentDateTime().toTimeZone(self.system_timezone)
        default_local = QDateTime(
            current_local.date(),
            QTime(current_local.time().hour(), current_local.time().minute()),
            self.system_timezone,
        )
        self._started_original_text = session.started_at if session else None
        self._completed_original_text = session.completed_at if session else None

        self.title_edit = QLineEdit(session.title if session else "")
        self.title_edit.setAccessibleName("Session title")
        self.title_edit.setPlaceholderText("Required")
        self.register_field("title", self.title_edit)

        self.description_edit = QPlainTextEdit(session.description if session else "")
        self.description_edit.setAccessibleName("Session description")
        self.description_edit.setMinimumHeight(76)

        started_value = self._stored_to_local(self._started_original_text, default_local)
        self.started_edit = self._new_datetime_edit(started_value, "Session started at")
        self.started_edit.setToolTip("Local system time in 24-hour format; saved as canonical UTC ISO-8601 text.")
        self.register_field("started_at", self.started_edit)

        completed_value = self._stored_to_local(self._completed_original_text, started_value)
        self.completed_edit = self._new_datetime_edit(completed_value, "Session completed at")
        self.completed_edit.setToolTip("Local system time in 24-hour format; saved as canonical UTC ISO-8601 text.")
        self.register_field("completed_at", self.completed_edit)
        self.completed_checkbox = QCheckBox("Set completion time")
        self.completed_checkbox.setAccessibleName("Set session completion time")
        self.completed_checkbox.setChecked(self._completed_original_text not in (None, ""))
        self.completed_edit.setEnabled(self.completed_checkbox.isChecked())
        completion_row = QWidget()
        completion_layout = QHBoxLayout(completion_row)
        completion_layout.setContentsMargins(0, 0, 0, 0)
        completion_layout.setSpacing(8)
        completion_layout.addWidget(self.completed_checkbox)
        completion_layout.addWidget(self.completed_edit, 1)

        self.notes_edit = QPlainTextEdit(session.notes if session else "")
        self.notes_edit.setAccessibleName("Session notes")
        self.notes_edit.setMinimumHeight(112)

        self.form.addRow("Title *", self.title_edit)
        self.form.addRow("Description", self.description_edit)
        self.form.addRow("Started (local, 24-hour) *", self.started_edit)
        self.form.addRow("Completed (local, 24-hour)", completion_row)
        self.form.addRow("Notes", self.notes_edit)

        for field in (self.title_edit,):
            field.textChanged.connect(self.mark_dirty)
        for field in (self.description_edit, self.notes_edit):
            field.textChanged.connect(self.mark_dirty)
        self.started_edit.dateTimeChanged.connect(self.mark_dirty)
        self.completed_edit.dateTimeChanged.connect(self.mark_dirty)
        self.completed_checkbox.toggled.connect(self.completed_edit.setEnabled)
        self.completed_checkbox.toggled.connect(self.mark_dirty)

        self._started_initial_msecs = self.started_edit.dateTime().toMSecsSinceEpoch() if self._started_original_text else None
        self._completed_initial_msecs = self.completed_edit.dateTime().toMSecsSinceEpoch() if self._completed_original_text else None

    def _new_datetime_edit(self, value: QDateTime, accessible_name: str) -> QDateTimeEdit:
        editor = QDateTimeEdit(value)
        editor.setAccessibleName(accessible_name)
        editor.setDisplayFormat(self.DATE_TIME_DISPLAY_FORMAT)
        editor.setCalendarPopup(True)
        editor.setKeyboardTracking(False)
        editor.setTimeZone(self.system_timezone)
        return editor

    def _stored_to_local(self, value: str | None, fallback: QDateTime) -> QDateTime:
        if value in (None, ""):
            return fallback
        parsed = parse_utc_timestamp(value)
        if parsed is None:
            return fallback
        epoch_msecs = int(parsed.timestamp() * 1000)
        return QDateTime.fromMSecsSinceEpoch(epoch_msecs, self.system_timezone)

    def _editor_utc_datetime(self, editor: QDateTimeEdit) -> datetime:
        local_value = editor.dateTime().toTimeZone(self.system_timezone)
        utc_value = local_value.toUTC()
        total_msecs = utc_value.toMSecsSinceEpoch()
        seconds, milliseconds = divmod(total_msecs, 1000)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=milliseconds * 1000)

    def _serialize_editor_value(
        self,
        editor: QDateTimeEdit,
        original_text: str | None,
        initial_msecs: int | None,
    ) -> str:
        current_msecs = editor.dateTime().toTimeZone(self.system_timezone).toMSecsSinceEpoch()
        if original_text not in (None, "") and initial_msecs == current_msecs:
            parsed = parse_utc_timestamp(original_text)
            if parsed is not None:
                return serialize_utc_timestamp(parsed)
            # Keep an already-invalid stored value visible to the engine's
            # authoritative validator unless the user changes the field.
            return original_text
        return serialize_utc_timestamp(self._editor_utc_datetime(editor))

    def build_draft(self) -> BenchmarkSession:
        started_at = self._serialize_editor_value(
            self.started_edit,
            self._started_original_text,
            self._started_initial_msecs,
        )
        completed_at = None
        if self.completed_checkbox.isChecked():
            completed_at = self._serialize_editor_value(
                self.completed_edit,
                self._completed_original_text,
                self._completed_initial_msecs,
            )
        if self.session is not None:
            return replace(
                self.session,
                title=self.title_edit.text(),
                description=self.description_edit.toPlainText(),
                started_at=started_at,
                completed_at=completed_at,
                notes=self.notes_edit.toPlainText(),
            )
        return BenchmarkSession(
            title=self.title_edit.text(),
            description=self.description_edit.toPlainText(),
            started_at=started_at,
            completed_at=completed_at,
            notes=self.notes_edit.toPlainText(),
        )

    def save_draft(self, draft: BenchmarkSession) -> BenchmarkSession:
        if draft.id is None:
            return self.context.catalog.create_session(draft)
        return self.context.catalog.update_session(draft)


__all__ = ("SessionEditorDialog",)
