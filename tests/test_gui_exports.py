from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QLabel

from engine.domain import BenchmarkRun, ReviewScore, ScoreboardEntry, ScoreboardImportBatch
from engine.standard_exports import StandardExportKind, StandardExportStatus, StandardExportWriteResult
from gui.context import GuiApplicationContext
from gui.dialogs.standard_export import StandardExportDialog
from gui.main_window import MainWindow


class StandardExportGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-standard-export-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.context = GuiApplicationContext.create(database_path=self.root / "data" / "benchmark.db")
        self.run, _score = self.context.benchmarks.save_run(
            BenchmarkRun(
                raw_model_output="GUI output",
                model_snapshot={"model_name": "Alpha", "backend": "LM Studio"},
                benchmark_snapshot={"name": "Review benchmark", "benchmark_type": "code_review"},
                prompt_snapshot={"name": "Prompt", "prompt_text": "Review."},
            ),
            ReviewScore(
                run_id=1,
                accuracy_score=4.0,
                hallucination_level="Low",
                reliability_level="High",
                overall_score=4.0,
            ),
        )
        batch = self.context.catalog.scoreboard_import_batches.create(
            ScoreboardImportBatch(name="GUI batch", source_file="gui.csv")
        )
        self.context.catalog.scoreboard_entries.create(
            ScoreboardEntry(model_name="Alpha", score=4.5, import_batch_id=batch.id, source_file="gui.csv")
        )

    def tearDown(self) -> None:
        self.context.close()
        self.directory.cleanup()

    def _dialog(
        self,
        kind: StandardExportKind = StandardExportKind.BENCHMARK_RUNS_CSV,
        *,
        confirm: bool = True,
    ) -> StandardExportDialog:
        dialog = StandardExportDialog(self.context, confirm_overwrite=lambda: confirm)
        dialog.format_combo.setCurrentIndex(dialog.format_combo.findData(kind.value))
        dialog.destination_edit.setText(str(self.root / f"{kind.value}.output"))
        dialog.show()
        self.application.processEvents()
        return dialog

    def test_real_preview_and_export_handlers_write_and_persist_only_after_success(self) -> None:
        dialog = self._dialog()
        try:
            self.assertFalse((self.root / "benchmark_runs_csv.output.csv").exists())
            self.assertFalse(self.context.settings.path.exists())
            dialog.preview_button.click()
            self.application.processEvents()
            self.assertIsNotNone(dialog._preview)
            self.assertEqual(dialog._preview.status, StandardExportStatus.SUCCESS)  # type: ignore[union-attr]
            self.assertTrue(dialog.export_button.isEnabled())
            self.assertFalse(self.context.settings.path.exists())
            dialog.export_button.click()
            self.application.processEvents()
            output = self.root / "benchmark_runs_csv.output.csv"
            self.assertTrue(output.exists())
            self.assertTrue(self.context.settings.path.exists())
            preferences = self.context.settings.get_export_preferences()
            self.assertEqual(preferences.last_export_kind, StandardExportKind.BENCHMARK_RUNS_CSV.value)
            self.assertEqual(preferences.last_export_directory, self.root.resolve())
            self.assertTrue(dialog.open_file_button.isVisible())
            self.assertTrue(dialog.open_folder_button.isVisible())
        finally:
            dialog.reject()

    def test_changing_format_or_scope_invalidates_preview_and_updates_untouched_filename(self) -> None:
        dialog = StandardExportDialog(self.context)
        try:
            old_destination = dialog.destination_edit.text()
            dialog.preview_button.click()
            self.assertTrue(dialog.export_button.isEnabled())
            dialog.format_combo.setCurrentIndex(dialog.format_combo.findData(StandardExportKind.SCOREBOARD_CSV.value))
            self.assertIsNone(dialog._preview)
            self.assertFalse(dialog.export_button.isEnabled())
            self.assertTrue(dialog.destination_edit.text().endswith("scoreboard.csv"))
            self.assertNotEqual(dialog.destination_edit.text(), old_destination)
            dialog.preview_button.click()
            self.assertTrue(dialog._preview is not None)
            dialog.model_filter_combo.setCurrentIndex(1)
            self.assertIsNone(dialog._preview)
            self.assertFalse(dialog.export_button.isEnabled())
        finally:
            dialog.reject()

    def test_cancel_and_preview_perform_zero_file_or_settings_writes(self) -> None:
        dialog = self._dialog()
        target = self.root / "cancelled.csv"
        try:
            dialog.destination_edit.setText(str(target))
            dialog.preview_button.click()
            self.assertFalse(target.exists())
            self.assertFalse(self.context.settings.path.exists())
            dialog.reject()
            self.assertFalse(target.exists())
            self.assertFalse(self.context.settings.path.exists())
        finally:
            dialog.close()

        configuration_cancel = self._dialog()
        try:
            configuration_cancel.reject()
            self.assertFalse(self.context.settings.path.exists())
        finally:
            configuration_cancel.close()

    def test_destination_browse_is_read_only_and_uses_a_valid_start_folder(self) -> None:
        dialog = self._dialog()
        selected = self.root / "browsed.csv"
        try:
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(selected), "CSV files")) as browse:
                dialog.browse_button.click()
            self.assertEqual(Path(dialog.destination_edit.text()), selected)
            self.assertEqual(Path(browse.call_args.args[2]).parent, self.root.resolve())
            self.assertFalse(self.context.settings.path.exists())
            self.assertFalse(selected.exists())
        finally:
            dialog.reject()

    def test_failed_write_keeps_configuration_and_does_not_persist_preferences(self) -> None:
        dialog = self._dialog()
        try:
            dialog.preview_button.click()
            destination_before = dialog.destination_edit.text()
            failure = StandardExportWriteResult(
                StandardExportStatus.TEMPORARY_WRITE_FAILED,
                StandardExportKind.BENCHMARK_RUNS_CSV,
                Path(destination_before),
                1,
                message="Could not write export.",
            )
            with patch.object(self.context.standard_exports, "write", return_value=failure):
                dialog.export_button.click()
            self.assertEqual(dialog.destination_edit.text(), destination_before)
            self.assertIn("could not", dialog.status_label.text().lower())
            self.assertFalse(self.context.settings.path.exists())
        finally:
            dialog.reject()

    def test_overwrite_decline_writes_nothing_and_confirmed_overwrite_succeeds(self) -> None:
        target = self.root / "existing.csv"
        target.write_text("keep", encoding="utf-8")
        declined = self._dialog(confirm=False)
        try:
            declined.destination_edit.setText(str(target))
            declined.preview_button.click()
            declined.export_button.click()
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")
            self.assertFalse(self.context.settings.path.exists())
            self.assertIn("declined", declined.status_label.text().lower())
        finally:
            declined.reject()

        confirmed = self._dialog(confirm=True)
        try:
            confirmed.destination_edit.setText(str(target))
            confirmed.preview_button.click()
            confirmed.export_button.click()
            self.assertIn("Alpha", target.read_text(encoding="utf-8"))
            self.assertTrue(self.context.settings.path.exists())
        finally:
            confirmed.reject()

    def test_invalid_remembered_kind_and_directory_fall_back_and_preserve_other_settings(self) -> None:
        self.context.settings.path.parent.mkdir(parents=True, exist_ok=True)
        self.context.settings.path.write_text(
            json.dumps(
                {
                    "keep": {"value": 7},
                    "attachment_preferences": {"storage_mode": "reference"},
                    "export_preferences": {
                        "last_export_directory": str(self.root / "missing"),
                        "last_export_kind": "not-a-real-kind",
                    },
                }
            ),
            encoding="utf-8",
        )
        dialog = StandardExportDialog(self.context)
        try:
            self.assertEqual(dialog._kind(), StandardExportKind.BENCHMARK_RUNS_CSV)
            self.assertTrue(Path(dialog.destination_edit.text()).parent.is_dir())
            dialog.preview_button.click()
            dialog.export_button.click()
            data = json.loads(self.context.settings.path.read_text(encoding="utf-8"))
            self.assertEqual(data["keep"], {"value": 7})
            self.assertEqual(data["attachment_preferences"], {"storage_mode": "reference"})
            self.assertEqual(data["export_preferences"]["last_export_kind"], "benchmark_runs_csv")
        finally:
            dialog.reject()

    def test_open_file_and_folder_failures_are_non_fatal_after_success(self) -> None:
        dialog = self._dialog()
        try:
            dialog.preview_button.click()
            dialog.export_button.click()
            with patch("gui.dialogs.standard_export.QDesktopServices.openUrl", return_value=False), patch(
                "gui.dialogs.standard_export.QMessageBox.warning"
            ) as warning:
                dialog.open_file_button.click()
                dialog.open_folder_button.click()
            self.assertEqual(warning.call_count, 2)
            self.assertTrue(dialog.result_label.isVisible())
        finally:
            dialog.reject()

    def test_jsonl_is_visible_as_a_dataset_builder_handoff(self) -> None:
        dialog = StandardExportDialog(self.context)
        try:
            index = dialog.format_combo.findData(StandardExportKind.JSONL_TRAINING_DATA.value)
            self.assertGreaterEqual(index, 0)
            self.assertTrue(dialog.format_combo.model().item(index).isEnabled())  # type: ignore[attr-defined]
            dialog.format_combo.setCurrentIndex(index)
            self.assertFalse(dialog.dataset_builder_group.isHidden())
            self.assertFalse(dialog.preview_button.isEnabled())
            self.assertFalse(dialog.export_button.isEnabled())
            self.assertIn("Dataset Builder", dialog.status_label.text())
            with patch.object(self.context.standard_exports, "preview") as preview, patch.object(
                self.context.standard_exports, "write"
            ) as write:
                dialog.preview()
                dialog.export()
            preview.assert_not_called()
            write.assert_not_called()
        finally:
            dialog.reject()
        window = MainWindow(self.context)
        try:
            self.assertIn("Dataset Builder", " ".join(label.text() for label in window.exports.findChildren(QLabel)))
            self.assertIs(window.pages["dataset_builder"], window.dataset_builder)
        finally:
            window.close()

    def test_jsonl_handoff_closes_dialog_and_navigates_to_stable_dataset_builder_page(self) -> None:
        window = MainWindow(self.context)
        try:
            dialog = StandardExportDialog(self.context, window)
            dialog.dataset_builder_requested.connect(window.open_dataset_builder)
            dialog.format_combo.setCurrentIndex(
                dialog.format_combo.findData(StandardExportKind.JSONL_TRAINING_DATA.value)
            )
            dialog.open_dataset_builder_button.click()
            self.assertEqual(window.current_page_key(), "dataset_builder")
            self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        finally:
            window.close()

    def test_page_and_header_launch_the_same_workflow_without_duplicate_connections(self) -> None:
        window = MainWindow(self.context)
        try:
            fake_dialog = MagicMock()
            fake_dialog.exec.return_value = QDialog.DialogCode.Rejected
            with patch("gui.main_window.StandardExportDialog", return_value=fake_dialog) as dialog_type:
                window.navigate_to("reports")
                window.exports.export_button.click()
                window.export_button.click()
            self.assertEqual(dialog_type.call_count, 2)
            self.assertEqual(fake_dialog.export_succeeded.connect.call_count, 2)
            self.assertEqual(fake_dialog.dataset_builder_requested.connect.call_count, 2)
            self.assertIs(window.pages["reports"], window.exports)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
