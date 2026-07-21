from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton

from engine.datasets import DatasetWriteResult, DatasetWriteStatus
from engine.domain import BenchmarkRun, ReviewScore
from gui.context import GuiApplicationContext
from gui.main_window import MainWindow
from gui.views.dataset_builder import DatasetBuilderState, DatasetBuilderView


class DatasetBuilderGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-dataset-builder-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.context = GuiApplicationContext.create(database_path=self.root / "data" / "benchmark.db")
        self.run, _review = self.context.benchmarks.save_run(
            BenchmarkRun(
                raw_model_output="Useful model output with alice@example.com",
                model_snapshot={
                    "model_name": "Alpha",
                    "backend": "LM Studio",
                    "temperature": 0.3,
                    "top_p": 0.9,
                },
                benchmark_snapshot={
                    "name": "Review benchmark",
                    "file_path": "review.py",
                    "benchmark_type": "code_review",
                },
                prompt_snapshot={"name": "Prompt", "prompt_text": "Review this result."},
                hardware_snapshot={"name": "Rig A", "gpu": "GPU"},
                created_at="2026-07-10T12:00:00+00:00",
            ),
            ReviewScore(
                run_id=0,
                accuracy_score=4.0,
                hallucination_level="Low",
                reliability_level="High",
                depth_score=4.0,
                signal_noise_score=4.0,
                actionability_score=4.0,
                seniority_score=4.0,
                overall_score=4.5,
                verdict="Approved",
            ),
        )
        self.source_snapshot = deepcopy(self.run.__dict__)

    def tearDown(self) -> None:
        self.context.close()
        self.directory.cleanup()

    def _view(
        self,
        *,
        confirm_build: bool = True,
        confirm_overwrite: bool = True,
    ) -> DatasetBuilderView:
        view = DatasetBuilderView(
            self.context,
            confirm_build=lambda _details: confirm_build,
            confirm_overwrite=lambda _path: confirm_overwrite,
        )
        view.show()
        self.application.processEvents()
        return view

    def _preview(self, view: DatasetBuilderView) -> None:
        view.preview_button.click()
        self.application.processEvents()
        self.assertEqual(view._state, DatasetBuilderState.PREVIEW_READY)

    def test_context_and_navigation_use_one_dataset_builder_page_with_validation_placeholder(self) -> None:
        self.assertIs(self.context.dataset_builder.service, self.context.benchmarks)
        window = MainWindow(self.context)
        try:
            self.assertIsInstance(window.pages["dataset_builder"], DatasetBuilderView)
            original = window.pages["dataset_builder"]
            window.navigate_to("dataset_builder")
            window.navigate_to("dashboard")
            window.navigate_to("dataset_builder")
            self.assertIs(window.pages["dataset_builder"], original)
            placeholder = window.dataset_builder.findChild(QLabel, "datasetValidationComingSoon")
            self.assertIsNotNone(placeholder)
            self.assertIn("5D2A-2B", placeholder.text())  # type: ignore[union-attr]
        finally:
            window.close()

    def test_invalid_remembered_directory_falls_back_without_writing_settings(self) -> None:
        self.context.settings.path.parent.mkdir(parents=True, exist_ok=True)
        original = {
            "keep": {"value": 3},
            "attachment_preferences": {"storage_mode": "reference"},
            "dataset_builder_preferences": {"last_dataset_directory": str(self.root / "missing")},
        }
        self.context.settings.path.write_text(json.dumps(original), encoding="utf-8")
        view = self._view()
        try:
            self.assertEqual(Path(view.destination_edit.text()).parent, self.root.resolve())
            self.assertEqual(json.loads(self.context.settings.path.read_text(encoding="utf-8")), original)
        finally:
            view.close()

    def test_date_checkboxes_control_date_editors_without_changing_other_behavior(self) -> None:
        view = self._view()
        try:
            self.assertFalse(view.date_from_edit.isEnabled())
            self.assertFalse(view.date_to_edit.isEnabled())
            view.date_from_check.click()
            view.date_to_check.click()
            self.assertTrue(view.date_from_edit.isEnabled())
            self.assertTrue(view.date_to_edit.isEnabled())
            view.date_from_check.click()
            view.date_to_check.click()
            self.assertFalse(view.date_from_edit.isEnabled())
            self.assertFalse(view.date_to_edit.isEnabled())
        finally:
            view.close()

    def test_duplicate_and_provenance_checkboxes_map_to_typed_dataset_filters(self) -> None:
        view = self._view()
        try:
            view.keep_duplicates_check.setChecked(True)
            view.include_provenance_check.setChecked(False)
            with patch.object(self.context.dataset_builder, "preview", wraps=self.context.dataset_builder.preview) as preview:
                self._preview(view)
            filters = preview.call_args.kwargs["filters"]
            self.assertTrue(filters.keep_source_duplicates)
            self.assertFalse(filters.include_provenance)
        finally:
            view.close()

    def test_filter_and_redaction_controls_feed_typed_preview_and_keep_source_immutable(self) -> None:
        view = self._view()
        try:
            view.min_overall_edit.setText("4.0")
            view.max_hallucination_combo.setCurrentText("Medium")
            view.min_reliability_combo.setCurrentText("Medium")
            view.verdict_edit.setText("approved")
            view.benchmark_type_combo.setCurrentText("Code Review")
            view.date_from_check.setChecked(True)
            view.date_from_edit.setDate(QDate(2026, 1, 1))
            view.date_to_check.setChecked(True)
            view.date_to_edit.setDate(QDate(2026, 12, 31))
            view.include_run_ids_edit.setText(str(self.run.id))
            view.exclude_run_ids_edit.setText("99999")
            view.include_provenance_check.setChecked(False)
            view.redact_usernames_check.setChecked(True)
            regex_edit = view.findChild(QLineEdit, "datasetRegexEdit")
            regex_add = view.findChild(QPushButton, "datasetRegexAddButton")
            self.assertIsNotNone(regex_edit)
            self.assertIsNotNone(regex_add)
            regex_edit.setText(r"token-\d+")  # type: ignore[union-attr]
            regex_add.click()  # type: ignore[union-attr]

            with patch.object(self.context.dataset_builder, "preview", wraps=self.context.dataset_builder.preview) as preview:
                self._preview(view)
                filters = preview.call_args.kwargs["filters"]
                redaction = preview.call_args.kwargs["redaction_config"]

            self.assertEqual(filters.min_overall, 4.0)
            self.assertEqual(filters.max_hallucination, "Medium")
            self.assertEqual(filters.min_reliability, "Medium")
            self.assertEqual(filters.verdict, "approved")
            self.assertEqual(filters.benchmark_type, "code_review")
            self.assertEqual(filters.include_run_ids, frozenset({self.run.id}))
            self.assertEqual(filters.exclude_run_ids, frozenset({99999}))
            self.assertFalse(filters.include_provenance)
            self.assertTrue(redaction.redact_usernames)
            self.assertEqual(redaction.regex_patterns, (r"token-\d+",))
            self.assertEqual(view.preview_model.rowCount(), 1)
            self.assertEqual(view.preview_model.data(view.preview_model.index(0, 0)), "Not recorded")
            self.assertEqual(view.preview_model.data(view.preview_model.index(0, 1)), "Alpha")
            self.assertEqual(self.run.__dict__, self.source_snapshot)
        finally:
            view.close()

    def test_invalid_regex_is_recoverable_and_rule_editor_can_remove_it(self) -> None:
        view = self._view()
        try:
            regex_edit = view.findChild(QLineEdit, "datasetRegexEdit")
            regex_add = view.findChild(QPushButton, "datasetRegexAddButton")
            regex_remove = view.findChild(QPushButton, "datasetRegexRemoveButton")
            self.assertIsNotNone(regex_edit)
            self.assertIsNotNone(regex_add)
            self.assertIsNotNone(regex_remove)
            regex_edit.setText("[")  # type: ignore[union-attr]
            regex_add.click()  # type: ignore[union-attr]
            view.preview_button.click()
            self.application.processEvents()
            self.assertEqual(view._state, DatasetBuilderState.RECOVERABLE_FAILURE)
            self.assertIn("invalid custom regex", view.status_label.text().lower())
            self.assertFalse((self.root / "dataset.jsonl").exists())
            self.assertFalse(self.context.settings.path.exists())
            view.regex_list.setCurrentRow(0)
            regex_remove.click()  # type: ignore[union-attr]
            self.assertEqual(view.regex_list.count(), 0)
        finally:
            view.close()

    def test_preview_has_zero_writes_and_destination_change_keeps_semantic_preview(self) -> None:
        view = self._view()
        try:
            self._preview(view)
            signature = view._preview_signature
            target_without_suffix = self.root / "built"
            view.destination_edit.setText(str(target_without_suffix))
            self.assertEqual(view._preview_signature, signature)
            self.assertTrue(view.manifest_path_label.text().endswith("built.jsonl.manifest.json"))
            self.assertFalse(target_without_suffix.exists())
            self.assertFalse((self.root / "built.jsonl").exists())
            self.assertFalse(self.context.settings.path.exists())
        finally:
            view.close()

    def test_empty_preview_disables_build_and_cancellation_writes_nothing(self) -> None:
        empty = self._view(confirm_build=False)
        try:
            empty.min_overall_edit.setText("5.0")
            self._preview(empty)
            self.assertEqual(empty.preview_model.rowCount(), 0)
            self.assertFalse(empty.build_button.isEnabled())
            empty.build_button.click()
            self.assertFalse((self.root / "dataset.jsonl").exists())
            self.assertFalse(self.context.settings.path.exists())
        finally:
            empty.close()

        cancelled = self._view(confirm_build=False)
        try:
            self._preview(cancelled)
            cancelled.build_button.click()
            self.assertIn("cancelled", cancelled.status_label.text().lower())
            self.assertFalse((self.root / "dataset.jsonl").exists())
            self.assertFalse(self.context.settings.path.exists())
        finally:
            cancelled.close()

    def test_successful_build_writes_jsonl_manifest_and_only_dataset_preferences(self) -> None:
        self.context.settings.path.parent.mkdir(parents=True, exist_ok=True)
        self.context.settings.path.write_text(
            json.dumps({"keep": {"value": 7}, "attachment_preferences": {"storage_mode": "reference"}}),
            encoding="utf-8",
        )
        view = self._view()
        try:
            destination = self.root / "curated.jsonl"
            view.destination_edit.setText(str(destination))
            self._preview(view)
            view.build_button.click()
            self.application.processEvents()
            manifest = destination.with_suffix(".jsonl.manifest.json")
            self.assertEqual(view._state, DatasetBuilderState.SUCCESS)
            self.assertTrue(destination.exists())
            self.assertTrue(manifest.exists())
            self.assertTrue(view.open_jsonl_button.isVisible())
            data = json.loads(self.context.settings.path.read_text(encoding="utf-8"))
            self.assertEqual(data["keep"], {"value": 7})
            self.assertEqual(data["attachment_preferences"], {"storage_mode": "reference"})
            self.assertEqual(data["dataset_builder_preferences"]["last_dataset_directory"], str(self.root.resolve()))
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["record_count"], 1)
            self.assertEqual(self.run.__dict__, self.source_snapshot)
        finally:
            view.close()

    def test_overwrite_requires_explicit_confirmation_and_retry_preserves_preview(self) -> None:
        destination = self.root / "existing.jsonl"
        destination.write_text("keep", encoding="utf-8")
        view = self._view(confirm_overwrite=False)
        try:
            view.destination_edit.setText(str(destination))
            self._preview(view)
            view.build_button.click()
            self.assertEqual(destination.read_text(encoding="utf-8"), "keep")
            self.assertEqual(view._state, DatasetBuilderState.PREVIEW_READY)
            self.assertIsNotNone(view._preview_signature)
            self.assertFalse(self.context.settings.path.exists())
            view._confirm_overwrite = lambda _path: True
            view.build_button.click()
            self.assertEqual(view._state, DatasetBuilderState.SUCCESS)
            self.assertIn("Useful model output", destination.read_text(encoding="utf-8"))
        finally:
            view.close()

    def test_source_change_invalidates_preview_without_accepting_stale_output(self) -> None:
        view = self._view()
        try:
            destination = self.root / "stale.jsonl"
            view.destination_edit.setText(str(destination))
            self._preview(view)
            self.context.benchmarks.delete_run(self.run.id)  # type: ignore[arg-type]
            view.build_button.click()
            self.assertEqual(view._state, DatasetBuilderState.STALE_PREVIEW)
            self.assertIn("source data changed", view.status_label.text().lower())
            self.assertFalse(view.open_jsonl_button.isVisible())
            self.assertFalse(destination.exists())
            self.assertFalse(self.context.settings.path.exists())
        finally:
            view.close()

    def test_all_non_success_write_statuses_keep_failures_recoverable_or_stale(self) -> None:
        cases = (
            (DatasetWriteStatus.TEMP_WRITE_FAILED, DatasetBuilderState.RECOVERABLE_FAILURE, True),
            (DatasetWriteStatus.TEMP_CLEANUP_FAILED, DatasetBuilderState.RECOVERABLE_FAILURE, True),
            (DatasetWriteStatus.JSONL_FINALIZE_FAILED, DatasetBuilderState.RECOVERABLE_FAILURE, True),
            (DatasetWriteStatus.VALIDATION_FAILED, DatasetBuilderState.STALE_PREVIEW, False),
            (DatasetWriteStatus.PARTIAL_FINALIZATION, DatasetBuilderState.STALE_PREVIEW, False),
        )
        for index, (status, expected_state, retryable) in enumerate(cases):
            with self.subTest(status=status):
                view = self._view()
                try:
                    destination = self.root / f"failure-{index}.jsonl"
                    view.destination_edit.setText(str(destination))
                    self._preview(view)
                    result = DatasetWriteResult(
                        status,
                        destination,
                        destination.with_suffix(".jsonl.manifest.json"),
                        message="simulated failure",
                        jsonl_finalized=status is DatasetWriteStatus.PARTIAL_FINALIZATION,
                    )
                    with patch.object(self.context.dataset_builder, "write_dataset", return_value=result):
                        view.build_button.click()
                    self.assertEqual(view._state, expected_state)
                    self.assertEqual(view._retryable_failure, retryable)
                    self.assertFalse(self.context.settings.path.exists())
                finally:
                    view.close()

    def test_open_services_fail_without_losing_success_state(self) -> None:
        view = self._view()
        try:
            self._preview(view)
            view.build_button.click()
            with patch("gui.views.dataset_builder.QDesktopServices.openUrl", return_value=False), patch(
                "gui.views.dataset_builder.QMessageBox.warning"
            ) as warning:
                view.open_jsonl_button.click()
                view.open_manifest_button.click()
                view.open_folder_button.click()
            self.assertEqual(warning.call_count, 3)
            self.assertEqual(view._state, DatasetBuilderState.SUCCESS)
        finally:
            view.close()

    def test_reports_exports_page_button_routes_to_dataset_builder(self) -> None:
        window = MainWindow(self.context)
        try:
            window.exports.dataset_builder_button.click()
            self.assertEqual(window.current_page_key(), "dataset_builder")
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
