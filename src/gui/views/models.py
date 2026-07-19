"""Model Profiles catalog page."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QDialog, QPushButton, QWidget

from ..context import GuiApplicationContext
from ..dialogs.model_editor import ModelEditorDialog
from ..models.catalog import display_bool, display_optional
from ..models.catalog_table_model import CatalogTableRow

try:
    from ...engine.domain import ModelProfile
except ImportError:  # pragma: no cover - exercised by top-level test imports.
    from engine.domain import ModelProfile  # type: ignore[no-redef]

from .catalog_page import CatalogPage


MODEL_HEADERS = (
    "Profile name",
    "Model name",
    "Family",
    "Size",
    "Quantization",
    "Backend",
    "Context",
    "Thinking",
    "Default",
)


class ModelsView(CatalogPage):
    def __init__(
        self,
        context: GuiApplicationContext,
        parent: QWidget | None = None,
        *,
        confirm_action: Callable[[str, str], bool] | None = None,
    ) -> None:
        super().__init__(
            context,
            title="Models",
            description="Manage reusable Model Profiles. The engine owns validation, default exclusivity, and historical run snapshots.",
            record_label="Model Profile",
            headers=MODEL_HEADERS,
            confirm_action=confirm_action,
            parent=parent,
        )
        self.make_default_button = QPushButton("Make Default")
        self.make_default_button.setObjectName("secondaryButton")
        self.make_default_button.setAccessibleName("Make selected Model Profile default")
        self.make_default_button.setToolTip("Use the engine service to make this the sole default profile.")
        self.make_default_button.setEnabled(False)
        self.make_default_button.clicked.connect(self.make_default_selected)
        self.action_layout.insertWidget(4, self.make_default_button)
        self._update_actions()

    def load_catalog_rows(self) -> tuple[CatalogTableRow[ModelProfile], ...]:
        rows = []
        for profile in self.context.catalog.list_model_profiles():
            context = display_optional(profile.context_length)
            default = "Default" if profile.is_default else "Not default"
            values = (
                profile.name,
                profile.model_name,
                display_optional(profile.model_family),
                display_optional(profile.model_size),
                display_optional(profile.quantization),
                profile.backend,
                context,
                display_bool(profile.thinking_enabled),
                default,
            )
            tooltips = (
                profile.name,
                profile.model_name,
                profile.model_family,
                profile.model_size,
                profile.quantization,
                profile.backend,
                str(profile.context_length) if profile.context_length is not None else "Not recorded",
                display_bool(profile.thinking_enabled),
                default,
            )
            rows.append(
                CatalogTableRow(
                    record=profile,
                    values=values,
                    tooltips=tooltips,
                    sort_values=(
                        profile.name.casefold(),
                        profile.model_name.casefold(),
                        profile.model_family.casefold(),
                        profile.model_size.casefold(),
                        profile.quantization.casefold(),
                        profile.backend.casefold(),
                        (1, -1) if profile.context_length is None else (0, profile.context_length),
                        1 if profile.thinking_enabled else 0,
                        0 if profile.is_default else 1,
                        profile.id or -1,
                    ),
                    search_text=" ".join(
                        (
                            profile.name,
                            profile.model_name,
                            profile.model_family,
                            profile.model_size,
                            profile.quantization,
                            profile.backend,
                        )
                    ).casefold(),
                )
            )
        return tuple(rows)

    def add_record(self) -> None:
        dialog = ModelEditorDialog(self.context, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.notify_changed("Model Profile created successfully.")
        dialog.deleteLater()

    def open_editor(self, record: ModelProfile) -> None:
        dialog = ModelEditorDialog(self.context, record, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.notify_changed("Model Profile updated successfully.")
        dialog.deleteLater()

    def update_entity_actions(self, record: ModelProfile | None) -> None:
        if not hasattr(self, "make_default_button"):
            return
        self.make_default_button.setEnabled(record is not None and not record.is_default)

    def make_default_selected(self) -> None:
        record = self.selected_record()
        if not isinstance(record, ModelProfile) or record.id is None or record.is_default:
            return
        if not self.confirm_action(
            "Make default Model Profile?",
            f'Make "{record.name}" the default Model Profile? Any existing default will be cleared by the engine.',
        ):
            return
        try:
            self.context.catalog.set_default_model_profile(record.id)
        except Exception as error:
            self.show_operation_error("made default", error)
            return
        self.notify_changed("Default Model Profile updated successfully.")


__all__ = ("MODEL_HEADERS", "ModelsView")
