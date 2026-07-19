"""Benchmark Definitions catalog page."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QDialog, QWidget

from ..context import GuiApplicationContext
from ..dialogs.benchmark_editor import BENCHMARK_TYPE_LABELS, BenchmarkEditorDialog
from ..models.catalog import display_timestamp, timestamp_sort_value
from ..models.catalog_table_model import CatalogTableRow

try:
    from ...engine.domain import BenchmarkDefinition
except ImportError:  # pragma: no cover - exercised by top-level test imports.
    from engine.domain import BenchmarkDefinition  # type: ignore[no-redef]

from .catalog_page import CatalogPage


BENCHMARK_HEADERS = ("Name", "Type", "File or target", "Tags", "Status", "Updated")


class BenchmarksView(CatalogPage):
    def __init__(
        self,
        context: GuiApplicationContext,
        parent: QWidget | None = None,
        *,
        confirm_action: Callable[[str, str], bool] | None = None,
    ) -> None:
        super().__init__(
            context,
            title="Benchmarks",
            description="Manage reusable Benchmark Definitions while keeping each run's captured benchmark snapshot authoritative.",
            record_label="Benchmark Definition",
            headers=BENCHMARK_HEADERS,
            lifecycle_options=(("Active", "active"), ("Inactive", "inactive"), ("All", "all")),
            confirm_action=confirm_action,
            parent=parent,
        )

    def load_catalog_rows(self) -> tuple[CatalogTableRow[BenchmarkDefinition], ...]:
        visibility = self.visibility_value()
        definitions = self.context.catalog.list_benchmark_definitions(include_inactive=True)
        if visibility == "active":
            definitions = [definition for definition in definitions if definition.is_active]
        elif visibility == "inactive":
            definitions = [definition for definition in definitions if not definition.is_active]
        rows = []
        for definition in definitions:
            type_label = BENCHMARK_TYPE_LABELS.get(definition.benchmark_type, definition.benchmark_type)
            status = "Active" if definition.is_active else "Inactive"
            values = (
                definition.name,
                type_label,
                definition.file_path,
                definition.tags or "Not recorded",
                status,
                display_timestamp(definition.updated_at),
            )
            tooltips = (
                definition.name,
                definition.benchmark_type,
                definition.file_path,
                definition.tags,
                status,
                definition.updated_at,
            )
            rows.append(
                CatalogTableRow(
                    record=definition,
                    values=values,
                    tooltips=tooltips,
                    sort_values=(
                        definition.name.casefold(),
                        type_label.casefold(),
                        definition.file_path.casefold(),
                        definition.tags.casefold(),
                        0 if definition.is_active else 1,
                        timestamp_sort_value(definition.updated_at),
                        definition.id or -1,
                    ),
                    search_text=" ".join(
                        (
                            definition.name,
                            definition.file_path,
                            definition.benchmark_type,
                            definition.tags,
                            definition.default_prompt,
                        )
                    ).casefold(),
                )
            )
        return tuple(rows)

    def add_record(self) -> None:
        dialog = BenchmarkEditorDialog(self.context, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.notify_changed("Benchmark Definition created successfully.")
        dialog.deleteLater()

    def open_editor(self, record: BenchmarkDefinition) -> None:
        dialog = BenchmarkEditorDialog(self.context, record, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.notify_changed("Benchmark Definition updated successfully.")
        dialog.deleteLater()

    def lifecycle_label(self, record: BenchmarkDefinition) -> str:
        return "Deactivate" if record.is_active else "Reactivate"

    def apply_lifecycle(self, record: BenchmarkDefinition) -> None:
        if record.id is None:
            return
        if record.is_active:
            if not self.confirm_action(
                "Deactivate benchmark?",
                f'Deactivate "{record.name}"? It will leave ordinary Add Run selection; historical snapshots remain readable.',
            ):
                return
            action = self.context.catalog.deactivate_benchmark_definition
            message = "Benchmark Definition deactivated successfully."
        else:
            action = self.context.catalog.reactivate_benchmark_definition
            message = "Benchmark Definition reactivated successfully."
        try:
            action(record.id)
        except Exception as error:
            self.show_operation_error("changed lifecycle", error)
            return
        self.notify_changed(message)


__all__ = ("BENCHMARK_HEADERS", "BenchmarksView")
