"""Hardware Profiles catalog page."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QDialog, QPushButton, QWidget

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
        self._delete_in_progress = False
        self.delete_button = QPushButton("Delete Hardware Profile")
        self.delete_button.setObjectName("secondaryButton")
        self.delete_button.setAccessibleName("Delete selected Hardware Profile")
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._on_delete_clicked)
        self.action_layout.insertWidget(self.action_layout.indexOf(self.refresh_button), self.delete_button)
        self._update_actions()

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

    def update_entity_actions(self, record: HardwareProfile | None) -> None:
        super().update_entity_actions(record)
        delete_button = getattr(self, "delete_button", None)
        if delete_button is None:
            return
        delete_button.setEnabled(
            not self._delete_in_progress and self.record_id(record) is not None
        )

    @staticmethod
    def _delete_confirmation_message(
        profile_name: str,
        active_run_count: int,
    ) -> str:
        if active_run_count == 0:
            return (
                f"Permanently delete the reusable hardware profile '{profile_name}'?\n\n"
                "No active benchmark runs reference this profile. Historical data is not affected.\n\n"
                "No in-app undo is available. Recovery requires an existing backup/restore copy."
            )
        run_label = "active benchmark run" if active_run_count == 1 else "active benchmark runs"
        return (
            f"Permanently delete the reusable hardware profile '{profile_name}'?\n\n"
            f"{active_run_count} {run_label} will remain. Their saved hardware snapshots will not change. "
            "Only the reusable hardware-profile links will be removed.\n\n"
            "This deletion is irreversible in the active database."
        )

    def _on_delete_clicked(self, _checked: bool = False) -> None:
        if self._delete_in_progress:
            return
        record = self.selected_record()
        profile_id = self.record_id(record)
        if profile_id is None:
            self._update_actions()
            return

        selected_row = self.catalog_table.currentIndex().row()
        self._delete_in_progress = True
        self._update_actions()
        try:
            try:
                preview = self.context.catalog.preview_hardware_profile_delete(profile_id)
            except KeyError:
                self.refresh()
                self.error_state.setText(
                    "The selected hardware profile is no longer available. The list was refreshed."
                )
                self.error_state.setVisible(True)
                return
            except Exception as error:
                self.show_operation_error("deleted", error)
                return

            if not self.confirm_action(
                "Delete Hardware Profile",
                self._delete_confirmation_message(preview.profile_name, preview.active_run_count),
            ):
                return

            try:
                result = self.context.catalog.delete_hardware_profile(profile_id)
            except KeyError:
                self.refresh()
                self.error_state.setText(
                    "The selected hardware profile is no longer available. The list was refreshed."
                )
                self.error_state.setVisible(True)
                return
            except Exception as error:
                self.show_operation_error("deleted", error)
                return

            self.refresh()
            if self.catalog_table.model().rowCount() > 0:
                row = min(max(selected_row, 0), self.catalog_table.model().rowCount() - 1)
                self.catalog_table.selectRow(row)
            self.catalog_changed.emit(self.record_label.casefold())
            active_run_label = (
                "active benchmark run" if result.detached_active_run_count == 1
                else "active benchmark runs"
            )
            if result.detached_active_run_count:
                message = (
                    f"Hardware Profile '{result.profile_name}' deleted. "
                    f"{result.detached_active_run_count} {active_run_label} remain with preserved snapshots."
                )
            else:
                message = f"Hardware Profile '{result.profile_name}' deleted."
            self.status_message.emit(message)
        finally:
            self._delete_in_progress = False
            self._update_actions()


__all__ = ("HARDWARE_PROFILE_HEADERS", "HardwareProfilesView")
