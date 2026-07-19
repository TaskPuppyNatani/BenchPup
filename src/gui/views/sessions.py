"""Sessions catalog page."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QDialog, QWidget

from ..context import GuiApplicationContext
from ..dialogs.session_editor import SessionEditorDialog
from ..models.catalog import display_optional, display_timestamp, timestamp_sort_value
from ..models.catalog_table_model import CatalogTableRow

try:
    from ...engine.domain import BenchmarkSession
except ImportError:  # pragma: no cover - exercised by top-level test imports.
    from engine.domain import BenchmarkSession  # type: ignore[no-redef]

from .catalog_page import CatalogPage


SESSION_HEADERS = ("Title", "Started (stored)", "Completed (stored)", "Status", "Runs", "Updated")


class SessionsView(CatalogPage):
    def __init__(
        self,
        context: GuiApplicationContext,
        parent: QWidget | None = None,
        *,
        confirm_action: Callable[[str, str], bool] | None = None,
    ) -> None:
        super().__init__(
            context,
            title="Sessions",
            description="Organize related benchmark work without changing the historical snapshots stored on runs.",
            record_label="Session",
            headers=SESSION_HEADERS,
            lifecycle_options=(("Active", "active"), ("Archived", "archived"), ("All", "all")),
            confirm_action=confirm_action,
            parent=parent,
        )

    def load_catalog_rows(self) -> tuple[CatalogTableRow[BenchmarkSession], ...]:
        visibility = self.visibility_value()
        sessions = self.context.catalog.list_sessions(include_deleted=True)
        if visibility == "active":
            sessions = [session for session in sessions if not session.is_deleted]
        elif visibility == "archived":
            sessions = [session for session in sessions if session.is_deleted]
        run_counts = self.context.catalog.session_run_counts()
        rows = []
        for session in sessions:
            run_count = run_counts.get(session.id or -1, 0)
            status = "Archived" if session.is_deleted else "Active"
            values = (
                session.title,
                display_timestamp(session.started_at),
                display_timestamp(session.completed_at),
                status,
                str(run_count),
                display_timestamp(session.updated_at),
            )
            tooltips = (
                session.title,
                display_optional(session.started_at),
                display_optional(session.completed_at),
                status,
                str(run_count),
                session.updated_at,
            )
            rows.append(
                CatalogTableRow(
                    record=session,
                    values=values,
                    tooltips=tooltips,
                    sort_values=(
                        session.title.casefold(),
                        timestamp_sort_value(session.started_at),
                        timestamp_sort_value(session.completed_at),
                        1 if session.is_deleted else 0,
                        run_count,
                        timestamp_sort_value(session.updated_at),
                        session.id or -1,
                    ),
                    search_text=" ".join((session.title, session.description, session.notes)).casefold(),
                )
            )
        return tuple(rows)

    def add_record(self) -> None:
        dialog = SessionEditorDialog(self.context, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.notify_changed("Session created successfully.")
        dialog.deleteLater()

    def open_editor(self, record: BenchmarkSession) -> None:
        dialog = SessionEditorDialog(self.context, record, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.notify_changed("Session updated successfully.")
        dialog.deleteLater()

    def lifecycle_label(self, record: BenchmarkSession) -> str:
        return "Restore" if record.is_deleted else "Archive"

    def apply_lifecycle(self, record: BenchmarkSession) -> None:
        if record.id is None:
            return
        if record.is_deleted:
            try:
                self.context.catalog.restore_session(record.id)
            except Exception as error:
                self.show_operation_error("restored", error)
                return
            self.notify_changed("Session restored successfully.")
            return
        if not self.confirm_action(
            "Archive session?",
            f'Archive "{record.title}"? Existing runs will remain readable, but the session will leave ordinary Add Run selection.',
        ):
            return
        try:
            self.context.catalog.archive_session(record.id)
        except Exception as error:
            self.show_operation_error("archived", error)
            return
        self.notify_changed("Session archived successfully.")


__all__ = ("SESSION_HEADERS", "SessionsView")
