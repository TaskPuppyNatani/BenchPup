from __future__ import annotations

import json
import codecs
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtWidgets import QApplication, QDialog, QWizard

from engine.domain import BenchmarkRun
from engine.importers import AMBIGUOUS_IMPORT, BENCHMARK_RUN_IMPORT, SCOREBOARD_IMPORT, UNSUPPORTED_IMPORT
from gui.context import GuiApplicationContext
from gui.dialogs.csv_import import CsvImportWizard
from gui.main_window import MainWindow


class Phase5D1ACsvImportGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-phase5d1a-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.context = GuiApplicationContext.create(database_path=self.root / "data" / "benchmark.db")

    def tearDown(self) -> None:
        self.context.close()
        self.directory.cleanup()

    def _csv(self, name: str, content: str, *, encoding: str = "utf-8") -> Path:
        path = self.root / name
        path.write_text(content, encoding=encoding)
        return path

    def _select_source(self, wizard: CsvImportWizard, path: Path) -> None:
        with patch(
            "gui.dialogs.csv_import.QFileDialog.getOpenFileName",
            return_value=(str(path), "CSV files (*.csv)"),
        ):
            wizard.browse_source()

    def _show(self, wizard: CsvImportWizard) -> None:
        wizard.show()
        self.application.processEvents()

    def _advance_to_commit(self, wizard: CsvImportWizard) -> None:
        self._show(wizard)
        for _ in range(4):
            if wizard.currentId() >= 3:
                break
            wizard.next()
            self.application.processEvents()
        self.assertEqual(wizard.currentId(), 3)

    def _wizard(self, path: Path, import_type: str | None = None) -> CsvImportWizard:
        wizard = CsvImportWizard(self.context, confirm_close=lambda: True)
        wizard.source_path_edit.setText(str(path))
        if import_type is not None:
            wizard.type_combo.setCurrentIndex(wizard.type_combo.findData(import_type))
        self.assertTrue(wizard._validate_source_page())
        wizard._prepare_mapping_page()
        self.assertTrue(wizard._validate_mapping_page())
        wizard._prepare_preview_page()
        return wizard

    def _commit(self, wizard: CsvImportWizard) -> None:
        self.assertTrue(wizard._validate_preview_page())
        wizard._prepare_commit_page()
        self.assertTrue(wizard._commit())

    def test_import_page_is_available_and_csv_dialog_can_be_created(self) -> None:
        window = MainWindow(self.context)
        try:
            self.assertIn("imports", window.pages)
            self.assertIs(window.page_stack.widget(window.page_indices["imports"]), window.imports)
            dialog = CsvImportWizard(self.context, window)
            self.assertEqual(dialog.windowTitle(), "Import CSV")
            self.assertIsNotNone(dialog.source_path_edit)
            self.assertIsNotNone(dialog.mapping_table)
            self.assertIsNotNone(dialog.source_preview_table)
            self.assertIsNotNone(dialog.mapped_preview_table)
            dialog.reject()
        finally:
            window.close()

    def test_real_wizard_blocks_ambiguous_detection_until_manual_type_selection(self) -> None:
        path = self._csv(
            "ambiguous.csv",
            "Model,Prompt Text,Raw Model Output\nQwen,prompt,output\n",
        )
        wizard = CsvImportWizard(self.context, confirm_close=lambda: True)
        try:
            self._select_source(wizard, path)
            self._show(wizard)
            wizard.next()
            self.application.processEvents()
            self.assertEqual(wizard.currentId(), 0)
            self.assertEqual(wizard.detection.status, AMBIGUOUS_IMPORT)  # type: ignore[union-attr]
            self.assertEqual(self.context.benchmarks.runs.list(), [])
            self.assertEqual(self.context.catalog.scoreboard_entries.list(), [])

            wizard.type_combo.setCurrentIndex(wizard.type_combo.findData(BENCHMARK_RUN_IMPORT))
            wizard.next()
            self.application.processEvents()
            self.assertEqual(wizard.currentId(), 1)
            wizard.next()
            self.application.processEvents()
            self.assertEqual(wizard.currentId(), 2)
            wizard.reject()
            self.assertEqual(self.context.benchmarks.runs.list(), [])
            self.assertEqual(self.context.catalog.scoreboard_import_batches.list(), [])
        finally:
            wizard.close()

    def test_real_wizard_rejects_missing_required_mapping_before_preview(self) -> None:
        path = self._csv(
            "missing-model-mapping.csv",
            "Model,Benchmark,Prompt Text,Raw Model Output\nQwen,main.py,prompt,output\n",
        )
        wizard = CsvImportWizard(self.context, confirm_close=lambda: True)
        try:
            self._select_source(wizard, path)
            wizard.type_combo.setCurrentIndex(wizard.type_combo.findData(BENCHMARK_RUN_IMPORT))
            self._show(wizard)
            wizard.next()
            self.application.processEvents()
            model_combo = wizard._mapping_controls["Model"]
            model_combo.setCurrentIndex(model_combo.findData(None))
            wizard.next()
            self.application.processEvents()
            self.assertEqual(wizard.currentId(), 1)
            self.assertIn("Required destination 'model_name'", wizard.mapping_status.text())
            self.assertEqual(self.context.benchmarks.runs.list(), [])
        finally:
            wizard.close()

    def test_real_wizard_rejects_duplicate_destination_mapping(self) -> None:
        path = self._csv(
            "duplicate-mapping.csv",
            "Model A,Model B,Benchmark,Prompt Text,Raw Model Output\nQwen,Other,main.py,prompt,output\n",
        )
        wizard = CsvImportWizard(self.context, confirm_close=lambda: True)
        try:
            self._select_source(wizard, path)
            wizard.type_combo.setCurrentIndex(wizard.type_combo.findData(BENCHMARK_RUN_IMPORT))
            self._show(wizard)
            wizard.next()
            self.application.processEvents()
            for heading in ("Model A", "Model B"):
                combo = wizard._mapping_controls[heading]
                combo.setCurrentIndex(combo.findData("model_name"))
            wizard.next()
            self.application.processEvents()
            self.assertEqual(wizard.currentId(), 1)
            self.assertIn("multiple source headings", wizard.mapping_status.text())
            self.assertEqual(self.context.benchmarks.runs.list(), [])
        finally:
            wizard.close()

    def test_real_wizard_utf32_is_unsupported_without_writes(self) -> None:
        path = self.root / "utf32.csv"
        path.write_bytes(codecs.BOM_UTF32_LE + "Model,Score\nQwen,4.5\n".encode("utf-32-le"))
        wizard = CsvImportWizard(self.context, confirm_close=lambda: True)
        try:
            self._select_source(wizard, path)
            self._show(wizard)
            wizard.next()
            self.application.processEvents()
            self.assertEqual(wizard.currentId(), 0)
            self.assertEqual(wizard.detection.status, UNSUPPORTED_IMPORT)  # type: ignore[union-attr]
            self.assertIn("not supported", wizard.source_status.text())
            self.assertEqual(self.context.benchmarks.runs.list(), [])
            self.assertEqual(self.context.catalog.scoreboard_import_batches.list(), [])
        finally:
            wizard.close()

    def test_real_wizard_cancellation_at_each_stage_performs_zero_writes(self) -> None:
        cases = (
            ("file-selection", None, None),
            ("type-selection", "Model,Prompt Text,Raw Model Output\nQwen,prompt,output\n", None),
            ("mapping", "Model,Benchmark,Prompt Text,Raw Model Output\nQwen,main.py,prompt,output\n", 1),
            ("preview", "Model,Benchmark,Prompt Text,Raw Model Output\nQwen,main.py,prompt,output\n", 2),
        )
        for name, content, page in cases:
            with self.subTest(stage=name):
                path = self._csv(f"cancel-{name}.csv", content) if content is not None else None
                wizard = CsvImportWizard(self.context, confirm_close=lambda: True)
                try:
                    if path is not None:
                        self._select_source(wizard, path)
                        self._show(wizard)
                        if name == "type-selection":
                            wizard.next()
                            self.application.processEvents()
                            self.assertEqual(wizard.currentId(), 0)
                        elif page is not None:
                            while wizard.currentId() < page:
                                wizard.next()
                                self.application.processEvents()
                    wizard.reject()
                    self.assertEqual(self.context.benchmarks.runs.list(), [])
                    self.assertEqual(self.context.catalog.scoreboard_import_batches.list(), [])
                    self.assertFalse(self.context.settings.path.exists())
                finally:
                    wizard.close()

    def test_real_wizard_finish_imports_scoreboard_and_prevents_repeat_commit(self) -> None:
        path = self._csv("real-scoreboard.csv", "Model,Score,Notes\nQwen,4.0,Historical result\n")
        wizard = CsvImportWizard(self.context, confirm_close=lambda: True)
        try:
            self._select_source(wizard, path)
            self._advance_to_commit(wizard)
            wizard.batch_name_edit.setText("Real wizard batch")
            wizard.batch_notes_edit.setPlainText("Committed through Finish")
            finish = wizard.button(QWizard.WizardButton.FinishButton)
            finish.click()
            self.application.processEvents()
            finish.click()
            self.application.processEvents()
            self.assertEqual(wizard.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(len(self.context.catalog.scoreboard_entries.list()), 1)
            self.assertEqual(len(self.context.catalog.scoreboard_import_batches.list()), 1)
            self.assertEqual(self.context.benchmarks.runs.list(), [])
        finally:
            wizard.close()

    def test_real_wizard_rolls_back_after_persistence_failure(self) -> None:
        path = self._csv(
            "persistence-failure.csv",
            "Model,Benchmark,Prompt Text,Raw Model Output\n"
            "Qwen,main.py,prompt,output one\n"
            "Llama,main.py,prompt,output two\n",
        )
        wizard = CsvImportWizard(self.context, confirm_close=lambda: True)
        try:
            self._select_source(wizard, path)
            wizard.type_combo.setCurrentIndex(wizard.type_combo.findData(BENCHMARK_RUN_IMPORT))
            self._advance_to_commit(wizard)
            original_values = self.context.benchmarks.runs._values
            calls = 0

            def fail_on_second(run: BenchmarkRun) -> dict[str, object]:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated persistence failure")
                return original_values(run)

            with patch.object(self.context.benchmarks.runs, "_values", side_effect=fail_on_second):
                wizard.button(QWizard.WizardButton.FinishButton).click()
                self.application.processEvents()
            self.assertEqual(self.context.benchmarks.runs.list(), [])
            self.assertIsNone(wizard.active_preview)
            self.assertIn("Import failed", wizard.commit_status.text())
            wizard.button(QWizard.WizardButton.FinishButton).click()
            self.application.processEvents()
            self.assertEqual(len(self.context.benchmarks.runs.list()), 2)
        finally:
            wizard.close()

    def test_real_scoreboard_wizard_rolls_back_batch_after_persistence_failure(self) -> None:
        path = self._csv(
            "scoreboard-persistence-failure.csv",
            "Model,Score,Notes\n"
            "Qwen,4.0,first\n"
            "Llama,3.0,second\n",
        )
        wizard = CsvImportWizard(self.context, confirm_close=lambda: True)
        try:
            self._select_source(wizard, path)
            self._advance_to_commit(wizard)
            original_values = self.context.catalog.scoreboard_entries._values
            calls = 0

            def fail_on_second(entry: object) -> dict[str, object]:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated scoreboard persistence failure")
                return original_values(entry)  # type: ignore[arg-type]

            with patch.object(self.context.catalog.scoreboard_entries, "_values", side_effect=fail_on_second):
                wizard.button(QWizard.WizardButton.FinishButton).click()
                self.application.processEvents()
            self.assertEqual(self.context.catalog.scoreboard_entries.list(), [])
            self.assertEqual(self.context.catalog.scoreboard_import_batches.list(), [])
            self.assertIn("Import failed", wizard.commit_status.text())
        finally:
            wizard.close()

    def test_imports_view_runs_real_wizard_and_refreshes_through_completion_signal(self) -> None:
        window = MainWindow(self.context)
        try:
            window.runs.refresh()
            path = self._csv(
                "view-refresh.csv",
                "Model,Benchmark,Prompt Text,Raw Model Output\nImported,main.py,prompt,output\n",
            )

            real_dialog: list[CsvImportWizard] = []

            def make_dialog(context: object, parent: object) -> CsvImportWizard:
                dialog = CsvImportWizard(context, parent)  # type: ignore[arg-type]
                real_dialog.append(dialog)
                return dialog

            def run_real_wizard() -> int:
                dialog = real_dialog[0]
                self._select_source(dialog, path)
                dialog.type_combo.setCurrentIndex(dialog.type_combo.findData(BENCHMARK_RUN_IMPORT))
                self._advance_to_commit(dialog)
                dialog.button(QWizard.WizardButton.FinishButton).click()
                self.application.processEvents()
                return int(QDialog.DialogCode.Accepted)

            with patch("gui.views.imports.CsvImportWizard", side_effect=make_dialog):
                with patch.object(CsvImportWizard, "exec", side_effect=run_real_wizard):
                    window.imports.open_csv_import()
            self.assertTrue(any(row.model == "Imported" for row in window.runs.rows))
        finally:
            window.close()

    def test_benchmark_run_import_uses_engine_and_keeps_records_separate(self) -> None:
        path = self._csv(
            "runs.csv",
            "Model,Benchmark,Prompt Text,Raw Model Output,Score\n"
            "Qwen,main.py,Review this,prompt output,4.5\n",
        )
        wizard = self._wizard(path)
        try:
            self.assertEqual(wizard.detection.import_type, BENCHMARK_RUN_IMPORT)  # type: ignore[union-attr]
            self.assertEqual(wizard._effective_import_type(), BENCHMARK_RUN_IMPORT)
            self._commit(wizard)
            runs = self.context.benchmarks.runs.list()
            self.assertEqual(len(runs), 1)
            _run, review, _attachments = self.context.benchmarks.get_run(runs[0].id or 0)
            self.assertIsNotNone(review)
            self.assertEqual(review.overall_score, 4.5)  # type: ignore[union-attr]
            self.assertEqual(self.context.catalog.scoreboard_entries.list(), [])
            self.assertEqual(wizard.import_result.imported, 1)  # type: ignore[union-attr]
        finally:
            wizard.close()

    def test_scoreboard_import_persists_batch_metadata_without_creating_runs(self) -> None:
        path = self._csv(
            "scoreboard.csv",
            "Model,Score,Notes\nQwen,4.0,Historical result\n",
        )
        wizard = self._wizard(path)
        try:
            self.assertEqual(wizard.detection.import_type, SCOREBOARD_IMPORT)  # type: ignore[union-attr]
            wizard.batch_name_edit.setText("July scoreboard")
            wizard.batch_notes_edit.setPlainText("Imported from the review sheet")
            self._commit(wizard)
            self.assertEqual(self.context.benchmarks.runs.list(), [])
            entries = self.context.catalog.scoreboard_entries.list()
            batches = self.context.catalog.scoreboard_import_batches.list()
            self.assertEqual(len(entries), 1)
            self.assertEqual((entries[0].model_name, entries[0].score), ("Qwen", 4.0))
            self.assertEqual((len(batches), batches[0].name, batches[0].notes), (1, "July scoreboard", "Imported from the review sheet"))
        finally:
            wizard.close()

    def test_manual_type_override_routes_scoreboard_shaped_csv_to_runs(self) -> None:
        path = self._csv("override.csv", "Model,Score,Notes\nQwen,4.0,Use as run\n")
        wizard = self._wizard(path, BENCHMARK_RUN_IMPORT)
        try:
            self.assertEqual(wizard.detection.import_type, SCOREBOARD_IMPORT)  # type: ignore[union-attr]
            self.assertEqual(wizard._effective_import_type(), BENCHMARK_RUN_IMPORT)
            self.assertEqual(wizard.active_preview.rows[0]["model_name"], "Qwen")  # type: ignore[union-attr]
            self._commit(wizard)
            self.assertEqual(len(self.context.benchmarks.runs.list()), 1)
            self.assertEqual(self.context.catalog.scoreboard_entries.list(), [])
        finally:
            wizard.close()

    def test_mapping_edit_and_source_mapped_previews_are_read_only(self) -> None:
        path = self._csv("mapping.csv", "Alias,Raw Model Output,Prompt Text\nQwen,pasted output,prompt\n")
        wizard = CsvImportWizard(self.context, confirm_close=lambda: True)
        try:
            wizard.source_path_edit.setText(str(path))
            wizard.type_combo.setCurrentIndex(wizard.type_combo.findData(BENCHMARK_RUN_IMPORT))
            self.assertTrue(wizard._validate_source_page())
            wizard._prepare_mapping_page()
            alias_combo = wizard._mapping_controls["Alias"]
            self.assertIsNone(alias_combo.currentData())
            alias_combo.setCurrentIndex(alias_combo.findData("model_name"))
            self.assertTrue(wizard._validate_mapping_page())
            wizard._prepare_preview_page()
            self.assertEqual(wizard.source_preview_table.model().rowCount(), 1)
            self.assertEqual(wizard.mapped_preview_table.model().rowCount(), 1)
            mapped_model = wizard.mapped_preview_table.model()
            self.assertIn("Qwen", str(mapped_model.data(mapped_model.index(0, 1))))
            self.assertEqual(self.context.benchmarks.runs.list(), [])
            self.assertEqual(self.context.catalog.scoreboard_entries.list(), [])
        finally:
            wizard.close()

    def test_preview_and_cancel_perform_zero_writes(self) -> None:
        path = self._csv("preview.csv", "Model,Benchmark,Prompt Text,Raw Model Output\nQwen,main.py,prompt,output\n")
        wizard = self._wizard(path)
        try:
            self.assertIsNone(wizard.import_result)
            self.assertEqual(self.context.benchmarks.runs.list(), [])
            self.assertFalse(self.context.settings.path.exists())
            wizard.reject()
            self.assertEqual(self.context.benchmarks.runs.list(), [])
            self.assertFalse(self.context.settings.path.exists())
        finally:
            wizard.close()

    def test_duplicate_policies_route_through_engine(self) -> None:
        path = self._csv("duplicates.csv", "Model,Benchmark,Prompt Text,Raw Model Output\nQwen,main.py,prompt,output\n")
        first = self._wizard(path, BENCHMARK_RUN_IMPORT)
        self._commit(first)
        first.close()

        skip = self._wizard(path, BENCHMARK_RUN_IMPORT)
        try:
            skip.duplicate_policy_combo.setCurrentIndex(skip.duplicate_policy_combo.findData("skip"))
            self._commit(skip)
            self.assertEqual((skip.import_result.imported, skip.import_result.skipped, skip.import_result.duplicates), (0, 1, 1))  # type: ignore[union-attr]
        finally:
            skip.close()

        replace = self._wizard(path, BENCHMARK_RUN_IMPORT)
        try:
            replace.duplicate_policy_combo.setCurrentIndex(replace.duplicate_policy_combo.findData("replace"))
            replace._prepare_preview_page()
            self._commit(replace)
            self.assertEqual((replace.import_result.imported, replace.import_result.replaced), (1, 1))  # type: ignore[union-attr]
        finally:
            replace.close()

        keep = self._wizard(path, BENCHMARK_RUN_IMPORT)
        try:
            keep.duplicate_policy_combo.setCurrentIndex(keep.duplicate_policy_combo.findData("keep"))
            keep._prepare_preview_page()
            self._commit(keep)
            self.assertEqual((keep.import_result.imported, keep.import_result.duplicates), (1, 1))  # type: ignore[union-attr]
            self.assertEqual(len(self.context.benchmarks.runs.list()), 2)
        finally:
            keep.close()

    def test_row_numbered_validation_blocks_commit_and_writes_nothing(self) -> None:
        path = self._csv(
            "invalid.csv",
            "Model,Context,Score\nGood,128k,4.0\n,,\nBad,not-a-context,4.0\n",
        )
        wizard = self._wizard(path, SCOREBOARD_IMPORT)
        try:
            self.assertFalse(wizard._validate_preview_page())
            self.assertIsNotNone(wizard.validation)
            self.assertEqual(wizard.active_preview.row_numbers, [2, 4])  # type: ignore[union-attr]
            self.assertEqual(wizard.validation.errors[0].row_number, 4)  # type: ignore[union-attr]
            self.assertFalse(wizard._commit())
            self.assertEqual(self.context.catalog.scoreboard_entries.list(), [])
            self.assertEqual(self.context.catalog.scoreboard_import_batches.list(), [])
        finally:
            wizard.close()

    def test_utf8_bom_is_reported_by_detection(self) -> None:
        path = self._csv(
            "bom.csv",
            "Model,Score\nQwen,4.5\n",
            encoding="utf-8-sig",
        )
        wizard = self._wizard(path, SCOREBOARD_IMPORT)
        try:
            self.assertEqual(wizard.encoding_value.text(), "utf-8-sig")
            self.assertEqual(wizard.detected_type_value.text(), "Scoreboard")
        finally:
            wizard.close()

    def test_invalid_remembered_settings_fall_back_safely(self) -> None:
        self.context.settings.path.parent.mkdir(parents=True, exist_ok=True)
        self.context.settings.path.write_text(
            json.dumps(
                {
                    "unrelated": "preserve",
                    "import_preferences": {
                        "source_directory": ["not-a-path"],
                        "import_type": ["not-a-type"],
                        "duplicate_policy": {"not": "a-policy"},
                    },
                }
            ),
            encoding="utf-8",
        )
        wizard = CsvImportWizard(self.context)
        try:
            self.assertEqual(wizard.type_combo.currentData(), "auto")
            self.assertEqual(wizard.duplicate_policy_combo.currentData(), "skip")
            self.assertIsNone(self.context.settings.get_import_preferences().source_directory)
            self.assertEqual(Path(wizard._start_directory()), self.context.paths.project_root)
            self.context.settings.set_import_preferences(self.context.settings.get_import_preferences())
            saved = json.loads(self.context.settings.path.read_text(encoding="utf-8"))
            self.assertEqual(saved["unrelated"], "preserve")
        finally:
            wizard.close()

    def test_success_refreshes_runs_and_preserves_existing_snapshots(self) -> None:
        window = MainWindow(self.context)
        try:
            existing, _score = self.context.benchmarks.save_run(
                BenchmarkRun(
                    raw_model_output="existing",
                    model_snapshot={"model_name": "Immutable model"},
                    benchmark_snapshot={"name": "Immutable benchmark"},
                    prompt_snapshot={"name": "Immutable prompt"},
                    hardware_snapshot={"name": "Immutable hardware"},
                )
            )
            before = {
                "model": dict(existing.model_snapshot),
                "benchmark": dict(existing.benchmark_snapshot),
                "prompt": dict(existing.prompt_snapshot),
                "hardware": dict(existing.hardware_snapshot),
            }
            path = self._csv(
                "refresh.csv",
                "Model,Benchmark,Prompt Text,Raw Model Output\nImported,main.py,prompt,output\n",
            )
            wizard = self._wizard(path, BENCHMARK_RUN_IMPORT)
            try:
                self._commit(wizard)
            finally:
                wizard.close()
            window.imports.import_completed.emit(BENCHMARK_RUN_IMPORT)
            self.assertTrue(any(row.model == "Imported" for row in window.runs.rows))
            refreshed, _review, _attachments = self.context.benchmarks.get_run(existing.id or 0)
            self.assertEqual(refreshed.model_snapshot, before["model"])  # type: ignore[union-attr]
            self.assertEqual(refreshed.benchmark_snapshot, before["benchmark"])  # type: ignore[union-attr]
            self.assertEqual(refreshed.prompt_snapshot, before["prompt"])  # type: ignore[union-attr]
            self.assertEqual(refreshed.hardware_snapshot, before["hardware"])  # type: ignore[union-attr]
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
