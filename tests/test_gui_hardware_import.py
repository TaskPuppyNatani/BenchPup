from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtWidgets import QApplication, QDialog

from engine.database import EngineDatabase
from engine.domain import BenchmarkRun, HardwareProfile
from engine.settings import AttachmentPreferences, HardwareImportPreferences
from engine.services import (
    CatalogService,
    HardwareProfileImportValidationError,
    HardwareProfileNameConflictError,
)
from gui.context import GuiApplicationContext
from gui.dialogs.hardware_import import HardwareImportDialog
from gui.main_window import MainWindow
from gui.views.add_run import AddRunWizard


class HardwareImportGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-hardware-import-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.context = GuiApplicationContext.create(database_path=self.root / "data" / "benchmark.db")
        self.widgets: list[object] = []

    def tearDown(self) -> None:
        for widget in self.widgets:
            widget.close()  # type: ignore[attr-defined]
        self.context.close()
        self.directory.cleanup()

    def _write_msinfo(self, name: str = "IMPORT-PC", cpu: str = "New CPU", gpu: str = "New GPU") -> Path:
        path = self.root / "reports" / "msinfo.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "System Information\n"
            f"System Name: {name}\n"
            "OS Name: Windows 11\n"
            f"Processor: {cpu}, details\n"
            "Installed Physical Memory (RAM): 64 GB\n"
            f"Adapter Description: {gpu}\n",
            encoding="utf-8",
        )
        return path

    def _write_dxdiag(self) -> Path:
        path = self.root / "reports" / "dxdiag.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "DxDiag\nMachine name: DX-PC\nOperating System: Windows 11\n"
            "Processor: DX CPU\nMemory: 32 GB RAM\nCard name: DX GPU\nDisplay Memory: 8 GB\n",
            encoding="utf-8",
        )
        return path

    def _write_lshw(self) -> Path:
        path = self.root / "reports" / "lshw.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "/0/0 processor Linux CPU\n/0/1 memory 16GiB System Memory\n"
            "/0/2 display Linux GPU\nhostname: LINUX-PC\n",
            encoding="utf-8",
        )
        return path

    def _open(self, source: Path, *, parser: str = "auto") -> HardwareImportDialog:
        dialog = HardwareImportDialog(self.context, confirm_close=lambda: True)
        self.widgets.append(dialog)
        parser_index = dialog.parser_combo.findData(parser)
        self.assertGreaterEqual(parser_index, 0)
        dialog.parser_combo.setCurrentIndex(parser_index)
        with patch(
            "gui.dialogs.hardware_import.QFileDialog.getOpenFileName",
            return_value=(str(source), ""),
        ):
            dialog.browse_button.click()
            self.application.processEvents()
        return dialog

    def test_auto_detect_imports_all_supported_parsers_and_persists_provenance(self) -> None:
        for source, expected_parser, expected_name in (
            (self._write_msinfo(), "MSInfo32", "IMPORT-PC"),
            (self._write_dxdiag(), "DXDiag", "DX-PC"),
            (self._write_lshw(), "lshw --short", "penguin"),
        ):
            if expected_name == "penguin":
                source.write_text(source.read_text(encoding="utf-8").replace("LINUX-PC", "penguin"), encoding="utf-8")
            with self.subTest(parser=expected_parser):
                dialog = self._open(source)
                self.assertTrue(dialog.save_button.isEnabled())
                self.assertEqual(dialog.provenance_source.text(), expected_parser)
                self.assertEqual(dialog.provenance_mode.text(), "Automatically detected")
                self.assertEqual(dialog.provenance_filename.text(), source.name)
                dialog.save_button.click()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                imported = next(
                    profile for profile in self.context.catalog.list_hardware_profiles()
                    if profile.import_source == expected_parser
                )
                self.assertEqual(imported.name, expected_name)
                self.assertIsNotNone(imported.imported_at)

        preferences = self.context.settings.get_hardware_import_preferences(
            self.context.hardware_importers.source_names()
        )
        self.assertEqual(preferences.source_directory, source.parent)
        self.assertEqual(preferences.parser_override, "auto")

    def test_ambiguous_detection_requires_explicit_parser_and_clears_state(self) -> None:
        source = self.root / "reports" / "ambiguous.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "System Information\nOS Name: Windows 11\nSystem Name: AMBIGUOUS\n"
            "Machine name: AMBIGUOUS\nOperating System: Windows 11\n",
            encoding="utf-8",
        )
        dialog = self._open(source)
        self.assertIn("Multiple hardware parsers matched", dialog.parser_status.text())
        self.assertFalse(dialog.save_button.isEnabled())

        dialog.parser_combo.setCurrentIndex(dialog.parser_combo.findData("DXDiag"))
        self.application.processEvents()
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertFalse(dialog.conflict_group.isVisible())
        self.assertFalse(dialog.warning_label.isVisible())
        self.assertFalse(dialog.error_label.isVisible())
        self.assertEqual(dialog.provenance_mode.text(), "Manually selected")

    def test_parser_change_clears_preview_conflicts_warnings_and_save_before_reparse(self) -> None:
        source = self.root / "reports" / "ambiguous-parser-change.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "System Information\nOS Name: Windows 11\nSystem Name: AMBIGUOUS\n"
            "Machine name: AMBIGUOUS\nOperating System: Windows 11\n",
            encoding="utf-8",
        )
        dialog = self._open(source)
        original = dialog.context.hardware_importers.parse_selected

        def parse_after_reset(parser_name: str, text: str):
            self.assertTrue(dialog.preview_group.isHidden())
            self.assertTrue(dialog.conflict_group.isHidden())
            self.assertTrue(dialog.warning_label.isHidden())
            self.assertTrue(dialog.error_label.isHidden())
            self.assertFalse(dialog.save_button.isEnabled())
            return original(parser_name, text)

        with patch.object(dialog.context.hardware_importers, "parse_selected", side_effect=parse_after_reset):
            dialog.parser_combo.setCurrentIndex(dialog.parser_combo.findData("DXDiag"))
        self.assertFalse(dialog.preview_group.isHidden())

    def test_preview_and_cancellation_write_neither_database_nor_settings(self) -> None:
        source = self._write_msinfo()
        self.context.settings.set_attachment_preferences(AttachmentPreferences(source_directory=self.root / "attachments"))
        settings_before = self.context.settings.path.read_bytes()
        before = self.context.catalog.list_hardware_profiles()
        dialog = self._open(source)
        self.assertEqual(self.context.catalog.list_hardware_profiles(), before)
        self.assertEqual(self.context.settings.path.read_bytes(), settings_before)
        dialog.reject()
        self.assertEqual(self.context.catalog.list_hardware_profiles(), [])
        self.assertEqual(self.context.settings.path.read_bytes(), settings_before)

    def test_cancellation_before_source_and_after_ambiguous_preview_writes_nothing(self) -> None:
        empty = HardwareImportDialog(self.context, confirm_close=lambda: True)
        self.widgets.append(empty)
        empty.reject()

        source = self.root / "reports" / "ambiguous.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "System Information\nOS Name: Windows 11\nMachine name: AMBIGUOUS\n"
            "Operating System: Windows 11\n",
            encoding="utf-8",
        )
        dialog = self._open(source)
        dialog.reject()
        self.assertEqual(self.context.catalog.list_hardware_profiles(), [])

    def test_empty_parse_cannot_save_until_meaningful_data_is_added(self) -> None:
        source = self.root / "reports" / "empty-dxdiag.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("DxDiag\n", encoding="utf-8")
        self.context.settings.set_attachment_preferences(AttachmentPreferences(storage_mode="reference"))
        settings_before = self.context.settings.path.read_bytes()
        dialog = self._open(source)
        self.assertFalse(dialog.warning_label.isHidden())
        self.assertFalse(dialog.save_button.isEnabled())
        assert dialog.fields_editor is not None
        dialog.fields_editor.notes_edit.setPlainText("only notes")
        self.assertFalse(dialog.save_button.isEnabled())
        with patch("gui.dialogs.hardware_import.QMessageBox.warning"):
            self.assertFalse(dialog._save())
        self.assertEqual(self.context.catalog.list_hardware_profiles(), [])
        self.assertEqual(self.context.settings.path.read_bytes(), settings_before)
        dialog.fields_editor.cpu_edit.setText("User supplied CPU")
        self.assertTrue(dialog.save_button.isEnabled())
        dialog.save_button.click()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(self.context.catalog.list_hardware_profiles()[0].cpu, "User supplied CPU")

    def test_import_validation_rejects_fallback_name_and_provenance_only(self) -> None:
        for profile in (
            HardwareProfile(name="DXDiag hardware"),
            HardwareProfile(name="Imported", import_source="DXDiag", imported_at="2026-07-19T00:00:00+00:00"),
            HardwareProfile(name="Imported", notes="only descriptive notes"),
            HardwareProfile(
                name="Imported",
                notes="only descriptive notes",
                import_source="DXDiag",
                imported_at="2026-07-19T00:00:00+00:00",
            ),
        ):
            with self.subTest(profile=profile):
                with self.assertRaises(HardwareProfileImportValidationError):
                    self.context.catalog.create_imported_hardware_profile(profile)
        self.assertEqual(self.context.catalog.list_hardware_profiles(), [])
        saved = self.context.catalog.create_imported_hardware_profile(
            HardwareProfile(name="Imported", notes="keep this", cpu="Real CPU")
        )
        self.assertEqual((saved.notes, saved.cpu), ("keep this", "Real CPU"))

    def test_database_failure_keeps_dialog_editable_and_does_not_write_settings(self) -> None:
        source = self._write_msinfo()
        self.context.settings.set_attachment_preferences(AttachmentPreferences(storage_mode="reference"))
        settings_before = self.context.settings.path.read_bytes()
        dialog = self._open(source)
        original_name = dialog.fields_editor.name_edit.text()  # type: ignore[union-attr]
        with patch("gui.dialogs.hardware_import.QMessageBox.warning"):
            with patch.object(
                self.context.catalog,
                "create_imported_hardware_profile",
                side_effect=RuntimeError("database unavailable"),
            ):
                dialog.save_button.click()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.fields_editor.name_edit.text(), original_name)  # type: ignore[union-attr]
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertEqual(self.context.catalog.list_hardware_profiles(), [])
        self.assertEqual(self.context.settings.path.read_bytes(), settings_before)

    def test_conflict_matching_normalizes_values_and_name_generation_is_deterministic(self) -> None:
        self.context.catalog.create_hardware_profile(
            HardwareProfile(name="Name", computer_name="Work Station", cpu="CPU", gpu="GPU")
        )
        self.context.catalog.create_hardware_profile(HardwareProfile(name="Name (2)"))
        candidate = HardwareProfile(
            name="  name ", computer_name="work   station", cpu=" CPU ", gpu="GPU"
        )
        conflicts = self.context.catalog.find_hardware_profile_conflicts(candidate)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].reasons, ("name", "computer_name", "cpu_gpu"))
        self.assertEqual(self.context.catalog.next_available_hardware_profile_name("Name"), "Name (3)")
        self.assertEqual(
            self.context.catalog.find_hardware_profile_conflicts(
                HardwareProfile(name="Unrelated", computer_name="", cpu="", gpu="")
            ),
            (),
        )

    def test_authoritative_import_creation_rejects_case_and_whitespace_name_collisions(self) -> None:
        self.context.catalog.create_imported_hardware_profile(HardwareProfile(name="Name", cpu="CPU"))
        with self.assertRaises(HardwareProfileNameConflictError):
            self.context.catalog.create_imported_hardware_profile(HardwareProfile(name=" name ", cpu="CPU 2"))

        self.context.catalog.create_imported_hardware_profile(HardwareProfile(name="Name X", cpu="CPU 3"))
        with self.assertRaises(HardwareProfileNameConflictError):
            self.context.catalog.create_imported_hardware_profile(HardwareProfile(name="  name   x  ", cpu="CPU 4"))
        self.assertEqual(len(self.context.catalog.list_hardware_profiles()), 2)
        self.assertEqual(self.context.catalog.next_available_hardware_profile_name("Name"), "Name (2)")

    def test_authoritative_import_recheck_uses_an_independent_database_connection(self) -> None:
        second_database = EngineDatabase(self.context.database.path)
        second_catalog = CatalogService(second_database)
        try:
            second_catalog.create_hardware_profile(
                HardwareProfile(name="Independent Writer", cpu="Concurrent CPU")
            )
            with self.assertRaises(HardwareProfileNameConflictError):
                self.context.catalog.create_imported_hardware_profile(
                    HardwareProfile(name=" independent   writer ", cpu="Imported CPU")
                )
        finally:
            second_database.close()

    def test_late_normalized_collision_preserves_dialog_and_settings(self) -> None:
        source = self._write_msinfo()
        self.context.settings.set_attachment_preferences(AttachmentPreferences(storage_mode="reference"))
        settings_before = self.context.settings.path.read_bytes()
        dialog = self._open(source)
        assert dialog.fields_editor is not None
        original_create = self.context.catalog.create_imported_hardware_profile

        def create_after_race(candidate: HardwareProfile) -> HardwareProfile:
            self.context.catalog.create_hardware_profile(
                HardwareProfile(name=" import-pc ", cpu="Concurrent CPU")
            )
            return original_create(candidate)

        with patch("gui.dialogs.hardware_import.QMessageBox.warning"):
            with patch.object(
                self.context.catalog,
                "create_imported_hardware_profile",
                side_effect=create_after_race,
            ):
                dialog.save_button.click()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.fields_editor.name_edit.text(), "IMPORT-PC")
        self.assertIn("normalized name", dialog.error_label.text())
        self.assertEqual(len(self.context.catalog.list_hardware_profiles()), 1)
        self.assertEqual(self.context.settings.path.read_bytes(), settings_before)

    def test_update_existing_uses_typed_conflict_resolution_and_preserves_created_at(self) -> None:
        existing = self.context.catalog.create_hardware_profile(
            HardwareProfile(name="IMPORT-PC", computer_name="IMPORT-PC", cpu="Old CPU", gpu="Old GPU")
        )
        source = self._write_msinfo()
        dialog = self._open(source)
        dialog.save_button.click()
        self.assertFalse(dialog.conflict_group.isHidden())
        self.assertEqual(dialog.conflict_combo.count(), 1)
        self.assertFalse(dialog.result() == QDialog.DialogCode.Accepted)
        dialog.conflict_combo.setCurrentIndex(0)
        dialog.update_existing_button.click()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        saved = self.context.catalog.get_hardware_profile(existing.id)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual((saved.cpu, saved.gpu, saved.created_at), ("New CPU", "New GPU", existing.created_at))

    def test_multiple_conflicts_show_reasons_and_create_new_uses_first_available_name(self) -> None:
        self.context.catalog.create_hardware_profile(
            HardwareProfile(name="IMPORT-PC", computer_name="OLD-PC", cpu="Other CPU", gpu="Other GPU")
        )
        self.context.catalog.create_hardware_profile(
            HardwareProfile(name="Other", computer_name="IMPORT-PC", cpu="Other CPU", gpu="Other GPU")
        )
        dialog = self._open(self._write_msinfo())
        dialog.save_button.click()
        self.assertEqual(dialog.conflict_combo.count(), 2)
        dialog.conflict_combo.setCurrentIndex(0)
        self.assertIn("Name", dialog.conflict_reason.text())
        dialog.create_new_button.click()
        self.assertEqual(dialog.fields_editor.name_edit.text(), "IMPORT-PC (2)")  # type: ignore[union-attr]
        dialog.save_button.click()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(len(self.context.catalog.list_hardware_profiles()), 3)

    def test_missing_and_unsupported_files_show_friendly_errors_and_keep_dialog_open(self) -> None:
        missing = self.root / "missing.txt"
        dialog = HardwareImportDialog(self.context, confirm_close=lambda: True)
        self.widgets.append(dialog)
        with patch("gui.dialogs.hardware_import.QMessageBox.warning") as warning:
            with patch(
                "gui.dialogs.hardware_import.QFileDialog.getOpenFileName",
                return_value=(str(missing), ""),
            ):
                dialog.browse_button.click()
        self.assertFalse(dialog.error_label.isHidden())
        warning.assert_called_once()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)

        unsupported = self.root / "utf32.txt"
        unsupported.write_bytes(b"\xff\xfe\x00\x00" + "hardware".encode("utf-32-le"))
        with patch("gui.dialogs.hardware_import.QMessageBox.warning"):
            with patch(
                "gui.dialogs.hardware_import.QFileDialog.getOpenFileName",
                return_value=(str(unsupported), ""),
            ):
                dialog.browse_button.click()
        self.assertIn("not supported", dialog.error_label.text())
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_hardware_file_dialog_uses_only_valid_remembered_directories(self) -> None:
        remembered = self.root / "remembered"
        remembered.mkdir()
        default = self.root / "default"
        default.mkdir()
        invalid = self.root / "missing"
        file_path = self.root / "not-a-directory.txt"
        file_path.write_text("file", encoding="utf-8")
        default_file = self.root / "default-file.txt"
        default_file.write_text("file", encoding="utf-8")

        cases = (
            (remembered, default, remembered),
            (invalid, default, default),
            (invalid, invalid, Path.cwd()),
            (file_path, default, default),
            (remembered, default_file, remembered),
            (invalid, default_file, Path.cwd()),
        )
        for remembered_directory, default_directory, expected in cases:
            with self.subTest(expected=expected):
                self.context.settings.set_hardware_import_preferences(
                    HardwareImportPreferences(source_directory=remembered_directory)
                )
                self.context.default_working_directory = default_directory
                dialog = HardwareImportDialog(self.context, confirm_close=lambda: True)
                self.widgets.append(dialog)
                with patch(
                    "gui.dialogs.hardware_import.QFileDialog.getOpenFileName",
                    return_value=("", ""),
                ) as chooser:
                    dialog.browse_button.click()
                self.assertEqual(Path(chooser.call_args.args[2]), expected)

        self.context.default_working_directory = default
        dialog = HardwareImportDialog(self.context, confirm_close=lambda: True)
        self.widgets.append(dialog)

        class BrokenDirectory:
            def is_dir(self) -> bool:
                raise OSError("filesystem inspection failed")

        dialog._last_source_directory = BrokenDirectory()  # type: ignore[assignment]
        with patch(
            "gui.dialogs.hardware_import.QFileDialog.getOpenFileName",
            return_value=("", ""),
        ) as chooser:
            dialog.browse_button.click()
        self.assertEqual(Path(chooser.call_args.args[2]), default)

    def test_no_valid_directory_keeps_dialog_open_without_opening_file_dialog(self) -> None:
        dialog = HardwareImportDialog(self.context, confirm_close=lambda: True)
        self.widgets.append(dialog)
        invalid = self.root / "deleted-current-directory"
        dialog._last_source_directory = invalid
        self.context.default_working_directory = invalid
        with patch("gui.dialogs.hardware_import.QMessageBox.warning") as warning:
            with patch.object(Path, "cwd", side_effect=OSError("current directory unavailable")):
                with patch(
                    "gui.dialogs.hardware_import.QFileDialog.getOpenFileName",
                ) as chooser:
                    dialog.browse_button.click()
        chooser.assert_not_called()
        warning.assert_called_once()
        self.assertIn("No accessible folder", dialog.error_label.text())
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_deleted_current_directory_is_not_passed_to_file_dialog(self) -> None:
        dialog = HardwareImportDialog(self.context, confirm_close=lambda: True)
        self.widgets.append(dialog)
        invalid = self.root / "deleted-current-directory"
        dialog._last_source_directory = invalid
        self.context.default_working_directory = invalid
        with patch("gui.dialogs.hardware_import.QMessageBox.warning") as warning:
            with patch.object(Path, "cwd", return_value=invalid):
                with patch(
                    "gui.dialogs.hardware_import.QFileDialog.getOpenFileName",
                    return_value=("", ""),
                ) as chooser:
                    dialog.browse_button.click()
        chooser.assert_not_called()
        warning.assert_called_once()

    def test_invalid_remembered_parser_falls_back_and_preserves_unrelated_settings(self) -> None:
        self.context.settings.set_attachment_preferences(AttachmentPreferences(storage_mode="reference"))
        self.context.settings.set_hardware_import_preferences(
            HardwareImportPreferences(source_directory=self.root, parser_override="Removed parser")
        )
        dialog = HardwareImportDialog(self.context, confirm_close=lambda: True)
        self.widgets.append(dialog)
        self.assertEqual(dialog.parser_combo.currentData(), "auto")
        self.assertEqual(self.context.settings.get_hardware_import_preferences().parser_override, "auto")
        data = json.loads(self.context.settings.path.read_text(encoding="utf-8"))
        self.assertEqual(data["attachment_preferences"]["storage_mode"], "reference")

    def test_success_signal_refreshes_loaded_hardware_profiles_view(self) -> None:
        window = MainWindow(self.context)
        self.widgets.append(window)
        self.assertTrue(window.hardware_profiles.has_loaded)
        dialog = self._open(self._write_dxdiag())
        dialog.import_completed.connect(window._handle_hardware_import_completed)
        dialog.save_button.click()
        self.assertEqual(window.hardware_profiles.catalog_table.model().rowCount(), 1)
        self.assertEqual(window.hardware_profiles.selected_record().id, dialog.saved_profile.id)  # type: ignore[union-attr]

    def test_imports_page_button_wires_real_dialog_signal_and_refreshes_active_add_run(self) -> None:
        window = MainWindow(self.context)
        self.widgets.append(window)
        window.navigate_to("imports")
        add_run = AddRunWizard(self.context, window)
        self.widgets.append(add_run)
        window._active_add_run = add_run
        created: list[HardwareProfile] = []

        def fake_exec(dialog: HardwareImportDialog) -> int:
            profile = self.context.catalog.create_hardware_profile(
                HardwareProfile(name=f"Imported {len(created) + 1}", cpu="Imported CPU")
            )
            created.append(profile)
            dialog.saved_profile = profile
            dialog.import_completed.emit(profile.id or -1)
            return QDialog.DialogCode.Accepted

        with patch.object(add_run, "refresh_catalog_choices", wraps=add_run.refresh_catalog_choices) as refresh:
            with patch.object(HardwareImportDialog, "exec", new=fake_exec):
                window.imports.import_hardware_button.click()
                window.imports.import_hardware_button.click()
                self.application.processEvents()
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(window.hardware_profiles.catalog_table.model().rowCount(), 2)
        self.assertGreaterEqual(add_run.hardware_combo.findData(created[-1].id), 0)
        self.assertEqual(window.imports.status_label.text(), "Hardware profile imported successfully.")
        window._active_add_run = None

    def test_import_updates_catalog_without_mutating_run_snapshot(self) -> None:
        original = self.context.catalog.create_hardware_profile(
            HardwareProfile(name="IMPORT-PC", computer_name="IMPORT-PC", cpu="Old CPU", gpu="Old GPU")
        )
        run, _ = self.context.benchmarks.save_run(BenchmarkRun(raw_model_output="captured", hardware_profile_id=original.id))
        snapshot = dict(run.hardware_snapshot)
        dialog = self._open(self._write_msinfo())
        dialog.save_button.click()
        dialog.conflict_combo.setCurrentIndex(0)
        dialog.update_existing_button.click()
        stored, _, _ = self.context.benchmarks.get_run(run.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.hardware_snapshot, snapshot)


if __name__ == "__main__":
    unittest.main()
