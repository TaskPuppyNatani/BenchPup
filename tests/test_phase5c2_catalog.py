from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtWidgets import QApplication, QLineEdit, QSizePolicy

from engine.domain import BenchmarkRun, HardwareProfile, PromptTemplate, prompt_hash_for
from engine.hardware_importers import DXDiagParser, LshwShortParser, MSInfo32Parser
from gui.context import GuiApplicationContext
from gui.dialogs.hardware_profile_editor import HardwareProfileEditorDialog
from gui.dialogs.prompt_template_editor import PromptTemplateEditorDialog
from gui.main_window import MainWindow
from gui.views.add_run import AddRunWizard
from gui.views.hardware_profiles import HardwareProfilesView
from gui.views.prompt_templates import PromptTemplatesView


class Phase5C2GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-phase5c2-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.context = GuiApplicationContext.create(
            database_path=Path(self.directory.name) / "data" / "benchmark.db"
        )
        self.widgets: list[object] = []

    def tearDown(self) -> None:
        for widget in self.widgets:
            widget.close()  # type: ignore[attr-defined]
        self.context.close()
        self.directory.cleanup()

    def _save_prompt(
        self,
        *,
        name: str = "Review prompt",
        version: str = "1.0",
        text: str = "Review the code.",
        benchmark_type: str = "code_review",
        notes: str = "",
    ) -> PromptTemplate:
        dialog = PromptTemplateEditorDialog(self.context, confirm_close=lambda: True)
        self.widgets.append(dialog)
        dialog.name_edit.setText(name)
        dialog.version_edit.setText(version)
        dialog.prompt_text_edit.setPlainText(text)
        dialog.type_combo.setCurrentIndex(dialog.type_combo.findData(benchmark_type))
        dialog.notes_edit.setPlainText(notes)
        self.assertTrue(dialog._save(), dialog.validation_summary.text())
        self.assertIsNotNone(dialog.saved_record)
        record = self.context.catalog.get_prompt_template(dialog.saved_record.id)
        self.assertIsNotNone(record)
        assert record is not None
        return record

    def _save_hardware(
        self,
        *,
        name: str = "Workstation",
        computer_name: str = "DESKTOP",
        cpu: str = "CPU",
        gpu: str = "GPU",
        vram: float | None = 24.0,
        ram: float | None = 64.0,
        operating_system: str = "Windows",
        backend_versions: dict[str, str] | None = None,
        notes: str = "",
    ) -> HardwareProfile:
        dialog = HardwareProfileEditorDialog(self.context, confirm_close=lambda: True)
        self.widgets.append(dialog)
        dialog.name_edit.setText(name)
        dialog.computer_name_edit.setText(computer_name)
        dialog.cpu_edit.setText(cpu)
        dialog.gpu_edit.setText(gpu)
        for field, value in ((dialog.vram_field, vram), (dialog.ram_field, ram)):
            field.record_checkbox.setChecked(value is not None)
            if value is not None:
                field.spin_box.setValue(value)
        dialog.operating_system_edit.setText(operating_system)
        for key, value in (backend_versions or {}).items():
            dialog.backend_versions_edit.add_row(key, value)
        dialog.notes_edit.setPlainText(notes)
        self.assertTrue(dialog._save(), dialog.validation_summary.text())
        self.assertIsNotNone(dialog.saved_record)
        record = self.context.catalog.get_hardware_profile(dialog.saved_record.id)
        self.assertIsNotNone(record)
        assert record is not None
        return record

    def test_prompt_template_empty_state_create_exact_text_and_engine_hash(self) -> None:
        page = PromptTemplatesView(self.context)
        self.widgets.append(page)
        self.assertEqual(page.rows, ())
        self.assertFalse(page.empty_state.isHidden())
        self.assertFalse(page.edit_button.isEnabled())
        self.assertFalse(page.lifecycle_button.isEnabled())

        text = "# Review\n\n  preserve indentation\n\n```python\nprint('exact')\n```\n"
        template = self._save_prompt(text=text, notes="Keep all whitespace")
        self.assertEqual(template.prompt_text, text)
        self.assertEqual(template.prompt_hash, prompt_hash_for(text))
        self.assertEqual(template.prompt_hash, hashlib.sha256(text.encode("utf-8")).hexdigest())
        template.validate()
        page.refresh()
        self.assertEqual(page.catalog_table.model().rowCount(), 1)
        self.assertEqual(page.rows[0].values[4], template.prompt_hash)

    def test_prompt_template_edit_rehashes_and_preserves_multiline_text(self) -> None:
        original = self._save_prompt(text="before\n  exact")
        dialog = PromptTemplateEditorDialog(self.context, original, confirm_close=lambda: True)
        self.widgets.append(dialog)
        replacement = "after\n\n    exact\nline 3"
        dialog.prompt_text_edit.setPlainText(replacement)
        self.assertTrue(dialog._save(), dialog.validation_summary.text())
        saved = self.context.catalog.get_prompt_template(original.id)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.prompt_text, replacement)
        self.assertEqual(saved.prompt_hash, prompt_hash_for(replacement))
        self.assertNotEqual(saved.prompt_hash, original.prompt_hash)

    def test_prompt_template_duplicate_and_invalid_validation_keep_editor_open(self) -> None:
        self._save_prompt(name="Unique", version="1", text="one")
        duplicate = PromptTemplateEditorDialog(self.context, confirm_close=lambda: True)
        invalid = PromptTemplateEditorDialog(self.context, confirm_close=lambda: True)
        self.widgets.extend((duplicate, invalid))

        duplicate.name_edit.setText("Unique")
        duplicate.version_edit.setText("1")
        duplicate.prompt_text_edit.setPlainText("replacement")
        self.assertFalse(duplicate._save())
        self.assertTrue(duplicate.isVisible() or not duplicate._saved)
        self.assertIn("already exists", duplicate.validation_summary.text())
        self.assertEqual(duplicate.prompt_text_edit.toPlainText(), "replacement")
        self.assertEqual(len(self.context.catalog.list_prompt_templates(include_inactive=True)), 1)

        invalid.name_edit.setText("Invalid type")
        invalid.version_edit.setText("1")
        invalid.prompt_text_edit.setPlainText("still exact")
        invalid.type_combo.setCurrentIndex(-1)
        self.assertFalse(invalid._save())
        self.assertIn("invalid benchmark_type", invalid.validation_summary.text())
        self.assertEqual(invalid.prompt_text_edit.toPlainText(), "still exact")

    def test_prompt_template_lifecycle_refresh_selection_and_active_add_run_choices(self) -> None:
        active = self._save_prompt(name="Active", version="1")
        other = self._save_prompt(name="Other", version="1")
        page = PromptTemplatesView(self.context, confirm_action=lambda _title, _message: True)
        self.widgets.append(page)
        self.assertTrue(page.select_record(other.id))
        self._save_prompt(name="New", version="1")
        page.refresh()
        selected = page.selected_record()
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, other.id)  # type: ignore[union-attr]

        page.select_record(active.id)
        page.apply_lifecycle(page.selected_record())
        self.assertFalse(self.context.catalog.get_prompt_template(active.id).is_active)  # type: ignore[union-attr]
        page.lifecycle_filter.setCurrentIndex(page.lifecycle_filter.findData("inactive"))  # type: ignore[union-attr]
        self.assertTrue(page.select_record(active.id))
        page.apply_lifecycle(page.selected_record())
        self.assertTrue(self.context.catalog.get_prompt_template(active.id).is_active)  # type: ignore[union-attr]

        wizard = AddRunWizard(self.context, confirm_close=lambda: True)
        self.widgets.append(wizard)
        self.assertGreaterEqual(wizard.prompt_template_combo.findData(active.id), 0)

    def test_prompt_template_cancel_writes_nothing(self) -> None:
        dialog = PromptTemplateEditorDialog(self.context, confirm_close=lambda: True)
        self.widgets.append(dialog)
        dialog.name_edit.setText("Cancelled")
        dialog.version_edit.setText("1")
        dialog.prompt_text_edit.setPlainText("not saved")
        dialog.reject()
        self.assertEqual(self.context.catalog.list_prompt_templates(include_inactive=True), [])

    def test_hardware_profile_empty_state_create_optional_values_and_mapping_round_trip(self) -> None:
        page = HardwareProfilesView(self.context)
        self.widgets.append(page)
        self.assertEqual(page.rows, ())
        self.assertFalse(page.empty_state.isHidden())
        profile = self._save_hardware(backend_versions={"LM Studio": "0.3", "Ollama": "0.2"})
        self.assertEqual(profile.backend_versions, {"LM Studio": "0.3", "Ollama": "0.2"})
        self.assertEqual(profile.vram_gb, 24.0)
        page.refresh()
        self.assertEqual(page.catalog_table.model().rowCount(), 1)
        self.assertEqual(page.rows[0].values[0], "Workstation")

        zero = self._save_hardware(name="Zero", vram=0.0, ram=None)
        self.assertEqual(zero.vram_gb, 0.0)
        self.assertIsNone(zero.ram_gb)
        self.assertEqual(self.context.catalog.list_hardware_profiles()[0].name, "Workstation")

    def test_backend_version_buttons_keep_string_api_and_consistent_editor_sizing(self) -> None:
        manual = HardwareProfileEditorDialog(self.context, confirm_close=lambda: True)
        imported = HardwareProfileEditorDialog(
            self.context,
            HardwareProfile(
                name="Imported rig",
                cpu="Imported CPU",
                backend_versions={"LM Studio": "1.2.3"},
                import_source="MSInfo32",
                imported_at="2026-07-18T12:00:00+00:00",
            ),
            confirm_close=lambda: True,
        )
        self.widgets.extend((manual, imported))

        editor = manual.backend_versions_edit
        editor.add_button.click()
        editor.add_button.click()
        self.assertEqual(editor.table.rowCount(), 2)

        editor.add_row("LM Studio", "1.2.3")
        self.assertEqual(editor.table.rowCount(), 3)
        backend = editor.table.cellWidget(2, 0)
        version = editor.table.cellWidget(2, 1)
        self.assertIsInstance(backend, QLineEdit)
        self.assertIsInstance(version, QLineEdit)
        assert isinstance(backend, QLineEdit)
        assert isinstance(version, QLineEdit)
        self.assertEqual(backend.text(), "LM Studio")
        self.assertEqual(version.text(), "1.2.3")

        row_heights = {editor.table.rowHeight(row) for row in range(editor.table.rowCount())}
        self.assertEqual(len(row_heights), 1)
        self.assertGreaterEqual(editor.table.verticalHeader().minimumSectionSize(), 30)
        self.assertGreaterEqual(editor.table.verticalHeader().defaultSectionSize(), 30)
        for row in range(editor.table.rowCount()):
            for column in (0, 1):
                widget = editor.table.cellWidget(row, column)
                self.assertIsInstance(widget, QLineEdit)
                assert isinstance(widget, QLineEdit)
                self.assertGreaterEqual(widget.minimumHeight(), widget.sizeHint().height())
                self.assertEqual(widget.sizePolicy().verticalPolicy(), QSizePolicy.Policy.Expanding)

        # The real remove button also receives clicked(bool); the selected row is removed cleanly.
        editor.remove_button.click()
        self.assertEqual(editor.table.rowCount(), 2)

        imported_editor = imported.backend_versions_edit
        existing_backend = imported_editor.table.cellWidget(0, 0)
        existing_version = imported_editor.table.cellWidget(0, 1)
        self.assertIsInstance(existing_backend, QLineEdit)
        self.assertIsInstance(existing_version, QLineEdit)
        assert isinstance(existing_backend, QLineEdit)
        assert isinstance(existing_version, QLineEdit)
        imported_editor.add_button.click()
        self.assertEqual(imported_editor.table.rowCount(), 2)
        self.assertEqual(imported_editor.table.rowHeight(0), editor.table.rowHeight(0))
        self.assertEqual(imported_editor.table.rowHeight(1), editor.table.rowHeight(0))
        self.assertEqual(existing_backend.text(), "LM Studio")
        self.assertEqual(existing_version.text(), "1.2.3")
        imported_editor.remove_button.click()
        self.assertEqual(imported_editor.mapping(), {"LM Studio": "1.2.3"})

    def test_hardware_profile_edit_preserves_imported_provenance_and_snapshot_fields(self) -> None:
        original = self.context.catalog.create_hardware_profile(
            HardwareProfile(
                name="Imported rig",
                computer_name="IMPORT-PC",
                cpu="Old CPU",
                gpu="Old GPU",
                vram_gb=12.5,
                ram_gb=32.0,
                operating_system="Windows 11",
                backend_versions={"LM Studio": "0.3"},
                import_source="MSInfo32",
                imported_at="2026-07-18T12:00:00+00:00",
            )
        )
        dialog = HardwareProfileEditorDialog(self.context, original, confirm_close=lambda: True)
        self.widgets.append(dialog)
        self.assertEqual(dialog.import_source_value.text(), "MSInfo32")
        self.assertIn("2026-07-18 12:00:00+00:00", dialog.imported_at_value.text())
        self.assertEqual(dialog.backend_versions_edit.mapping(), {"LM Studio": "0.3"})
        dialog.cpu_edit.setText("New CPU")
        dialog.backend_versions_edit.add_row("Ollama", "0.2")
        self.assertTrue(dialog._save(), dialog.validation_summary.text())
        saved = self.context.catalog.get_hardware_profile(original.id)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.cpu, "New CPU")
        self.assertEqual(saved.computer_name, "IMPORT-PC")
        self.assertEqual(saved.import_source, "MSInfo32")
        self.assertEqual(saved.imported_at, "2026-07-18T12:00:00+00:00")
        self.assertEqual(saved.backend_versions, {"LM Studio": "0.3", "Ollama": "0.2"})

    def test_hardware_profile_imported_msinfo32_dxdiag_and_lshw_records_round_trip(self) -> None:
        fixtures = (
            (
                "MSInfo32",
                MSInfo32Parser().parse(
                    "System Information\nSystem Name: MSINFO-PC\nOS Name: Windows 11\n"
                    "Processor: CPU, details\nInstalled Physical Memory (RAM): 64 GB\n"
                    "Adapter Description: GPU\n"
                ),
            ),
            (
                "DXDiag",
                DXDiagParser().parse(
                    "DxDiag\nMachine name: DX-PC\nOperating System: Windows 11\n"
                    "Processor: CPU\nMemory: 32 GB RAM\nCard name: GPU\nDisplay Memory: 8 GB\n"
                ),
            ),
            (
                "lshw --short",
                LshwShortParser().parse(
                    "/0/0 processor Linux CPU\n/0/1 memory 16GiB System Memory\n"
                    "/0/2 display Linux GPU\nhostname: LINUX-PC\n"
                ),
            ),
        )
        for source, draft in fixtures:
            with self.subTest(source=source):
                profile = self.context.catalog.create_hardware_profile(
                    HardwareProfile(
                        name=f"{source} profile",
                        computer_name=draft.computer_name,
                        cpu=draft.cpu,
                        gpu=draft.gpu,
                        vram_gb=draft.vram_gb,
                        ram_gb=draft.ram_gb,
                        operating_system=draft.operating_system,
                        backend_versions={"backend": "1.0"},
                        notes=draft.notes,
                        import_source=source,
                        imported_at="2026-07-18T12:00:00+00:00",
                    )
                )
                dialog = HardwareProfileEditorDialog(self.context, profile, confirm_close=lambda: True)
                self.widgets.append(dialog)
                self.assertEqual(dialog.backend_versions_edit.mapping(), {"backend": "1.0"})
                self.assertTrue(dialog._save(), dialog.validation_summary.text())
                saved = self.context.catalog.get_hardware_profile(profile.id)
                self.assertEqual(saved.import_source, source)  # type: ignore[union-attr]
                self.assertEqual(saved.imported_at, "2026-07-18T12:00:00+00:00")  # type: ignore[union-attr]
                self.assertEqual(saved.computer_name, draft.computer_name)  # type: ignore[union-attr]

    def test_hardware_profile_validation_failure_cancel_and_manual_provenance(self) -> None:
        invalid = HardwareProfileEditorDialog(self.context, confirm_close=lambda: True)
        self.widgets.append(invalid)
        invalid.name_edit.setText("Invalid mapping")
        invalid.backend_versions_edit.add_row("", "1.0")
        self.assertFalse(invalid._save())
        self.assertIn("backend version name is required", invalid.validation_summary.text())
        self.assertEqual(invalid.name_edit.text(), "Invalid mapping")
        self.assertEqual(invalid.backend_versions_edit.table.rowCount(), 1)

        cancelled = HardwareProfileEditorDialog(self.context, confirm_close=lambda: True)
        self.widgets.append(cancelled)
        cancelled.name_edit.setText("Cancelled hardware")
        cancelled.reject()
        self.assertEqual(self.context.catalog.list_hardware_profiles(), [])

        manual = self._save_hardware(name="Manual", backend_versions={})
        self.assertEqual(manual.import_source, "")
        self.assertIsNone(manual.imported_at)
        self.assertEqual(self.context.catalog.get_hardware_profile(manual.id).import_source, "")  # type: ignore[union-attr]

        with self.assertRaises(ValueError):
            self.context.catalog.create_hardware_profile(HardwareProfile(name="Negative", vram_gb=-1.0))

    def test_hardware_profile_refresh_selection_and_service_listing_order(self) -> None:
        first = self._save_hardware(name="Zulu")
        second = self._save_hardware(name="Alpha")
        page = HardwareProfilesView(self.context)
        self.widgets.append(page)
        self.assertEqual([row.record.name for row in page.rows], ["Alpha", "Zulu"])
        self.assertTrue(page.select_record(first.id))
        self._save_hardware(name="Middle")
        page.refresh()
        selected = page.selected_record()
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, first.id)  # type: ignore[union-attr]
        self.assertTrue(page.edit_button.isEnabled())
        self.assertEqual(second.name, "Alpha")

    def test_prompt_and_hardware_catalog_edits_do_not_mutate_historical_snapshots(self) -> None:
        prompt = self._save_prompt(text="Historical prompt")
        hardware = self._save_hardware(backend_versions={"Backend": "before"})
        run, _ = self.context.benchmarks.save_run(
            BenchmarkRun(
                raw_model_output="captured",
                prompt_template_id=prompt.id,
                hardware_profile_id=hardware.id,
            )
        )
        prompt_snapshot = dict(run.prompt_snapshot)
        hardware_snapshot = dict(run.hardware_snapshot)

        prompt_dialog = PromptTemplateEditorDialog(self.context, prompt, confirm_close=lambda: True)
        hardware_dialog = HardwareProfileEditorDialog(self.context, hardware, confirm_close=lambda: True)
        self.widgets.extend((prompt_dialog, hardware_dialog))
        prompt_dialog.prompt_text_edit.setPlainText("Changed later")
        hardware_dialog.gpu_edit.setText("Changed GPU")
        self.assertTrue(prompt_dialog._save(), prompt_dialog.validation_summary.text())
        self.assertTrue(hardware_dialog._save(), hardware_dialog.validation_summary.text())

        stored, _, _ = self.context.benchmarks.get_run(run.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.prompt_snapshot, prompt_snapshot)
        self.assertEqual(stored.hardware_snapshot, hardware_snapshot)

    def test_main_window_exposes_prompt_and_hardware_pages_and_reuses_one_context(self) -> None:
        window = MainWindow(self.context)
        self.widgets.append(window)
        self.assertIsInstance(window.prompt_templates, PromptTemplatesView)
        self.assertIsInstance(window.hardware_profiles, HardwareProfilesView)
        window.navigate_to("prompt_templates")
        self.assertIs(window.page_stack.currentWidget(), window.prompt_templates)
        window.navigate_to("hardware_profiles")
        self.assertIs(window.page_stack.currentWidget(), window.hardware_profiles)
        self.assertIs(window.prompt_templates.context, self.context)
        self.assertIs(window.hardware_profiles.context, self.context)


if __name__ == "__main__":
    unittest.main()
