"""Prompt Templates catalog page."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QDialog, QWidget

from ..context import GuiApplicationContext
from ..dialogs.prompt_template_editor import PromptTemplateEditorDialog
from ..models.catalog import display_optional, display_timestamp, timestamp_sort_value
from ..models.catalog_table_model import CatalogTableRow

try:
    from ...engine.domain import PromptTemplate
except ImportError:  # pragma: no cover - exercised by top-level test imports.
    from engine.domain import PromptTemplate  # type: ignore[no-redef]

from .catalog_page import CatalogPage


PROMPT_TEMPLATE_HEADERS = ("Name", "Version", "Benchmark type", "Status", "Prompt hash", "Updated")


class PromptTemplatesView(CatalogPage):
    def __init__(
        self,
        context: GuiApplicationContext,
        parent: QWidget | None = None,
        *,
        confirm_action: Callable[[str, str], bool] | None = None,
    ) -> None:
        super().__init__(
            context,
            title="Prompt Templates",
            description="Manage reusable exact-text prompt versions while keeping each run's captured prompt snapshot authoritative.",
            record_label="Prompt Template",
            headers=PROMPT_TEMPLATE_HEADERS,
            lifecycle_options=(
                ("Active", "active"),
                ("Inactive", "inactive"),
                ("All", "all"),
            ),
            confirm_action=confirm_action,
            parent=parent,
        )

    def load_catalog_rows(self) -> tuple[CatalogTableRow[PromptTemplate], ...]:
        visibility = self.visibility_value()
        templates = self.context.catalog.list_prompt_templates(include_inactive=True)
        if visibility == "active":
            templates = [template for template in templates if template.is_active]
        elif visibility == "inactive":
            templates = [template for template in templates if not template.is_active]
        rows = []
        for template in templates:
            status = "Active" if template.is_active else "Inactive"
            values = (
                template.name,
                template.version,
                template.benchmark_type,
                status,
                display_optional(template.prompt_hash),
                display_timestamp(template.updated_at),
            )
            tooltips = (
                template.name,
                template.version,
                template.benchmark_type,
                status,
                template.prompt_hash,
                template.updated_at,
            )
            rows.append(
                CatalogTableRow(
                    record=template,
                    values=values,
                    tooltips=tooltips,
                    sort_values=(
                        template.name.casefold(),
                        template.version.casefold(),
                        template.benchmark_type.casefold(),
                        0 if template.is_active else 1,
                        template.prompt_hash.casefold(),
                        timestamp_sort_value(template.updated_at),
                        template.id or -1,
                    ),
                    search_text=" ".join(
                        (
                            template.name,
                            template.version,
                            template.benchmark_type,
                            template.prompt_text,
                            template.notes,
                            template.prompt_hash,
                        )
                    ).casefold(),
                )
            )
        return tuple(rows)

    def add_record(self) -> None:
        dialog = PromptTemplateEditorDialog(self.context, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            saved_id = dialog.saved_record.id if dialog.saved_record is not None else None
            self.notify_changed("Prompt Template created successfully.", select_id=saved_id)
        dialog.deleteLater()

    def open_editor(self, record: PromptTemplate) -> None:
        dialog = PromptTemplateEditorDialog(self.context, record, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            saved_id = dialog.saved_record.id if dialog.saved_record is not None else record.id
            self.notify_changed("Prompt Template updated successfully.", select_id=saved_id)
        dialog.deleteLater()

    def lifecycle_label(self, record: PromptTemplate) -> str:
        return "Deactivate" if record.is_active else "Reactivate"

    def apply_lifecycle(self, record: PromptTemplate) -> None:
        if record.id is None:
            return
        if record.is_active:
            if not self.confirm_action(
                "Deactivate Prompt Template?",
                f'Deactivate "{record.name}" v{record.version}? It will leave ordinary Add Run selection; historical snapshots remain readable.',
            ):
                return
            action = self.context.catalog.deactivate_prompt_template
            message = "Prompt Template deactivated successfully."
        else:
            action = self.context.catalog.reactivate_prompt_template
            message = "Prompt Template reactivated successfully."
        try:
            action(record.id)
        except Exception as error:
            self.show_operation_error("changed lifecycle", error)
            return
        self.notify_changed(message)


__all__ = ("PROMPT_TEMPLATE_HEADERS", "PromptTemplatesView")
