"""Hardware Profiles catalog page."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QDialog, QWidget

from ..context import GuiApplicationContext
from ..dialogs.hardware_profile_editor import HardwareProfileEditorDialog
from ..models.catalog import display_optional, display_timestamp, timestamp_sort_value
from ..models.catalog_table_model import CatalogTableRow

try:
    from ...engine.domain import HardwareProfile
except ImportError:  # pragma: no cover - exercised by top-level test imports.
    from engine.domain import HardwareProfile  # type: ignore[no-redef]

from .catalog_page import CatalogPage


HARDWARE_PROFILE_HEADERS = (
    "Name",
    "Computer name",
    "CPU",
    "GPU",
    "VRAM (GB)",
    "RAM (GB)",
    "Operating system",
    "Import source",
    "Imported",
)


class HardwareProfilesView(CatalogPage):
    def __init__(
        self,
        context: GuiApplicationContext,
        parent: QWidget | None = None,
        *,
        confirm_action: Callable[[str, str], bool] | None = None,
    ) -> None:
        super().__init__(
            context,
            title="Hardware Profiles",
            description="Manage reusable hardware and backend metadata while keeping each run's captured hardware snapshot authoritative.",
            record_label="Hardware Profile",
            headers=HARDWARE_PROFILE_HEADERS,
            confirm_action=confirm_action,
            parent=parent,
        )

    def load_catalog_rows(self) -> tuple[CatalogTableRow[HardwareProfile], ...]:
        rows = []
        for profile in self.context.catalog.list_hardware_profiles():
            values = (
                profile.name,
                display_optional(profile.computer_name),
                display_optional(profile.cpu),
                display_optional(profile.gpu),
                display_optional(profile.vram_gb),
                display_optional(profile.ram_gb),
                display_optional(profile.operating_system),
                display_optional(profile.import_source, missing="Manual"),
                display_timestamp(profile.imported_at),
            )
            tooltips = (
                profile.name,
                profile.computer_name,
                profile.cpu,
                profile.gpu,
                str(profile.vram_gb) if profile.vram_gb is not None else "Not recorded",
                str(profile.ram_gb) if profile.ram_gb is not None else "Not recorded",
                profile.operating_system,
                profile.import_source or "Manual",
                profile.imported_at or "Not recorded",
            )
            versions = " ".join(f"{key}={value}" for key, value in profile.backend_versions.items())
            rows.append(
                CatalogTableRow(
                    record=profile,
                    values=values,
                    tooltips=tooltips,
                    sort_values=(
                        profile.name.casefold(),
                        profile.computer_name.casefold(),
                        profile.cpu.casefold(),
                        profile.gpu.casefold(),
                        (1, 0.0) if profile.vram_gb is None else (0, profile.vram_gb),
                        (1, 0.0) if profile.ram_gb is None else (0, profile.ram_gb),
                        profile.operating_system.casefold(),
                        profile.import_source.casefold(),
                        timestamp_sort_value(profile.imported_at),
                        profile.id or -1,
                    ),
                    search_text=" ".join(
                        (
                            profile.name,
                            profile.computer_name,
                            profile.cpu,
                            profile.gpu,
                            profile.operating_system,
                            profile.notes,
                            profile.import_source,
                            profile.imported_at or "",
                            versions,
                        )
                    ).casefold(),
                )
            )
        return tuple(rows)

    def add_record(self) -> None:
        dialog = HardwareProfileEditorDialog(self.context, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            saved_id = dialog.saved_record.id if dialog.saved_record is not None else None
            self.notify_changed("Hardware Profile created successfully.", select_id=saved_id)
        dialog.deleteLater()

    def open_editor(self, record: HardwareProfile) -> None:
        dialog = HardwareProfileEditorDialog(self.context, record, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            saved_id = dialog.saved_record.id if dialog.saved_record is not None else record.id
            self.notify_changed("Hardware Profile updated successfully.", select_id=saved_id)
        dialog.deleteLater()


__all__ = ("HARDWARE_PROFILE_HEADERS", "HardwareProfilesView")
