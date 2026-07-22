from __future__ import annotations

import json
import hashlib
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

from engine.datasets import (
    DATASET_FORMAT_VERSION,
    DATASET_VALIDATION_MAX_ISSUES,
    DatasetWriteResult,
    DatasetWriteStatus,
)
from engine.domain import BenchmarkRun, ReviewScore
from gui.context import GuiApplicationContext
from gui.main_window import MainWindow
from gui.views.dataset_builder import (
    DatasetBuilderState,
    DatasetBuilderView,
    DatasetValidationGuiState,
    DatasetValidationMode,
)


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

    def _standalone_dataset(
        self,
        name: str = "standalone.jsonl",
        *,
        records: tuple[dict[str, object], ...] | None = None,
        blank_line: bool = False,
    ) -> tuple[Path, Path]:
        dataset = self.root / name
        selected_records = records or (
            {
                "instruction": "Review the result",
                "input": {"model": "Alpha"},
                "response": {"overall": 4.5},
                "metadata": {"source": "test"},
            },
        )
        lines = [json.dumps(record) for record in selected_records]
        text = "\n".join(lines) + "\n"
        if blank_line:
            text += "\n"
        dataset.write_text(text, encoding="utf-8")
        manifest = dataset.with_suffix(dataset.suffix + ".manifest.json")
        manifest.write_text(
            json.dumps(
                {
                    "dataset_filename": dataset.name,
                    "record_count": len(selected_records),
                    "excluded_count": 0,
                    "duplicate_count": 0,
                    "redaction_count": 0,
                    "benchpup_version": "test",
                    "schema_version": 1,
                    "created_at": "2026-07-10T12:00:00+00:00",
                    "format_version": DATASET_FORMAT_VERSION,
                    "sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
                    "selected_filters": {"model": "Alpha"},
                }
            ),
            encoding="utf-8",
        )
        return dataset, manifest

    def _validation_view(self) -> DatasetBuilderView:
        view = self._view()
        view.tabs.setCurrentWidget(view._validation_tab)
        self.application.processEvents()
        return view

    @staticmethod
    def _select_validation_mode(view: DatasetBuilderView, mode: DatasetValidationMode) -> None:
        index = view.validation_mode_combo.findData(mode.value)
        assert index >= 0
        view.validation_mode_combo.setCurrentIndex(index)

    def test_context_and_navigation_use_one_dataset_builder_page_with_validation_tab(self) -> None:
        self.assertIs(self.context.dataset_builder.service, self.context.benchmarks)
        window = MainWindow(self.context)
        try:
            self.assertIsInstance(window.pages["dataset_builder"], DatasetBuilderView)
            original = window.pages["dataset_builder"]
            window.navigate_to("dataset_builder")
            window.navigate_to("dashboard")
            window.navigate_to("dataset_builder")
            self.assertIs(window.pages["dataset_builder"], original)
            self.assertIsNone(window.dataset_builder.findChild(QLabel, "datasetValidationComingSoon"))
            self.assertIsNotNone(window.dataset_builder.validation_mode_combo)
            self.assertEqual(
                window.dataset_builder._validation_state,
                DatasetValidationGuiState.EMPTY,
            )
        finally:
            window.close()

    def test_validation_modes_and_ready_state_are_reachable(self) -> None:
        view = self._validation_view()
        try:
            self.assertEqual(view._validation_state, DatasetValidationGuiState.EMPTY)
            self.assertTrue(view.validation_dataset_edit.isVisible())
            self.assertFalse(view.validation_manifest_edit.isVisible())
            self.assertFalse(view.validation_action_button.isEnabled())

            dataset, manifest = self._standalone_dataset()
            view.validation_dataset_edit.setText(str(dataset))
            self.assertEqual(view._validation_state, DatasetValidationGuiState.READY)
            view.validation_action_button.click()
            self.assertEqual(view._validation_state, DatasetValidationGuiState.VALID)

            self._select_validation_mode(view, DatasetValidationMode.MANIFEST)
            self.assertEqual(view._validation_state, DatasetValidationGuiState.EMPTY)
            self.assertFalse(view.validation_dataset_edit.isVisible())
            self.assertTrue(view.validation_manifest_edit.isVisible())
            self.assertEqual(view.validation_summary_label.text(), "No validation result.")
            view.validation_manifest_edit.setText(str(manifest))
            self.assertEqual(view._validation_state, DatasetValidationGuiState.READY)

            self._select_validation_mode(view, DatasetValidationMode.PAIR)
            self.assertEqual(view._validation_state, DatasetValidationGuiState.READY)
            self.assertTrue(view.validation_dataset_edit.isVisible())
            self.assertTrue(view.validation_manifest_edit.isVisible())
            self.assertTrue(view.use_adjacent_manifest_button.isVisible())
        finally:
            view.close()

    def test_valid_dataset_displays_counts_and_performs_no_writes(self) -> None:
        dataset, _manifest = self._standalone_dataset(blank_line=True)
        original_settings = (
            self.context.settings.path.read_bytes()
            if self.context.settings.path.exists()
            else None
        )
        original_file = dataset.read_bytes()
        view = self._validation_view()
        try:
            view.validation_dataset_edit.setText(str(dataset))
            view.validation_action_button.click()
            self.assertEqual(view._validation_state, DatasetValidationGuiState.VALID)
            self.assertEqual(view.validation_status_label.text(), "Valid dataset")
            summary = view.validation_summary_label.text()
            self.assertIn("Valid records: 1", summary)
            self.assertIn("Nonblank physical lines: 1", summary)
            self.assertIn("Blank lines accepted: 1", summary)
            self.assertEqual(view._validation_issue_model.rowCount(), 0)
            self.assertEqual(dataset.read_bytes(), original_file)
            self.assertEqual(
                self.context.settings.path.read_bytes()
                if self.context.settings.path.exists()
                else None,
                original_settings,
            )
            self.assertEqual(self.run.__dict__, self.source_snapshot)
        finally:
            view.close()

    def test_empty_dataset_is_valid_and_malformed_line_keeps_physical_number(self) -> None:
        empty = self.root / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        view = self._validation_view()
        try:
            view.validation_dataset_edit.setText(str(empty))
            view.validation_action_button.click()
            self.assertEqual(view._validation_state, DatasetValidationGuiState.VALID)
            self.assertEqual(view.validation_status_label.text(), "Valid empty dataset")
            self.assertIn("Valid records: 0", view.validation_summary_label.text())

            malformed = self.root / "malformed.jsonl"
            malformed.write_text(
                "\n"
                + json.dumps(
                    {
                        "instruction": "instruction",
                        "input": {},
                        "response": {},
                        "metadata": {},
                    }
                )
                + "\n{not valid json}\n",
                encoding="utf-8",
            )
            view.validation_dataset_edit.setText(str(malformed))
            view.validation_action_button.click()
            self.assertEqual(view._validation_state, DatasetValidationGuiState.INVALID)
            self.assertEqual(view._validation_issue_model.rowCount(), 1)
            self.assertEqual(view._validation_issue_model.data(view._validation_issue_model.index(0, 0)), "dataset")
            self.assertEqual(view._validation_issue_model.data(view._validation_issue_model.index(0, 1)), "malformed_json")
            self.assertEqual(view._validation_issue_model.data(view._validation_issue_model.index(0, 2)), "3")
        finally:
            view.close()

    def test_manifest_validation_displays_typed_fields_and_readable_metadata(self) -> None:
        _dataset, manifest = self._standalone_dataset()
        view = self._validation_view()
        try:
            self._select_validation_mode(view, DatasetValidationMode.MANIFEST)
            view.validation_manifest_edit.setText(str(manifest))
            view.validation_action_button.click()
            self.assertEqual(view._validation_state, DatasetValidationGuiState.VALID)
            self.assertEqual(view.validation_status_label.text(), "Valid manifest")
            summary = view.validation_summary_label.text()
            self.assertIn("Dataset filename: standalone.jsonl", summary)
            self.assertIn("Declared record count: 1", summary)
            self.assertIn("Format version: 1", summary)
            self.assertIn("Created at: 2026-07-10T12:00:00+00:00", summary)
            self.assertIn("selected_filters: model: Alpha", view.validation_metadata_label.text())
            self.assertNotIn("None", view.validation_summary_label.text())
            self.assertNotIn("None", view.validation_metadata_label.text())
        finally:
            view.close()

    def test_invalid_manifest_and_directory_results_are_recoverable_without_tracebacks(self) -> None:
        invalid_manifest = self.root / "invalid.manifest.json"
        invalid_manifest.write_text(json.dumps({"record_count": -1}), encoding="utf-8")
        view = self._validation_view()
        try:
            self._select_validation_mode(view, DatasetValidationMode.MANIFEST)
            view.validation_manifest_edit.setText(str(invalid_manifest))
            view.validation_action_button.click()
            self.assertEqual(view._validation_state, DatasetValidationGuiState.INVALID)
            self.assertGreater(view._validation_issue_model.rowCount(), 0)
            issue_values = [
                view._validation_issue_model.data(view._validation_issue_model.index(0, column))
                for column in range(view._validation_issue_model.columnCount())
            ]
            self.assertNotIn(None, issue_values)
            self.assertNotIn("Traceback", " ".join(str(value) for value in issue_values))

            self._select_validation_mode(view, DatasetValidationMode.DATASET)
            view.validation_dataset_edit.setText(str(self.root))
            view.validation_action_button.click()
            self.assertEqual(view._validation_state, DatasetValidationGuiState.RECOVERABLE_FAILURE)
            self.assertIn("could not be read", view.validation_status_label.text().lower())
            self.assertEqual(view.validation_dataset_edit.text(), str(self.root))
        finally:
            view.close()

    def test_dataset_issue_truncation_is_reported_without_fabricated_rows(self) -> None:
        dataset = self.root / "many-errors.jsonl"
        dataset.write_text("\n".join("{bad json}" for _ in range(DATASET_VALIDATION_MAX_ISSUES + 3)), encoding="utf-8")
        view = self._validation_view()
        try:
            view.validation_dataset_edit.setText(str(dataset))
            view.validation_action_button.click()
            self.assertEqual(view._validation_state, DatasetValidationGuiState.INVALID)
            self.assertEqual(view._validation_issue_model.rowCount(), DATASET_VALIDATION_MAX_ISSUES)
            self.assertIn("truncated", view.validation_issue_summary_label.text().lower())
            self.assertEqual(
                view._validation_issue_model.data(
                    view._validation_issue_model.index(DATASET_VALIDATION_MAX_ISSUES - 1, 2)
                ),
                str(DATASET_VALIDATION_MAX_ISSUES),
            )
        finally:
            view.close()

    def test_pair_validation_displays_matches_and_actual_declared_values(self) -> None:
        dataset, manifest = self._standalone_dataset()
        view = self._validation_view()
        try:
            self._select_validation_mode(view, DatasetValidationMode.PAIR)
            view.validation_dataset_edit.setText(str(dataset))
            view.validation_manifest_edit.setText(str(manifest))
            view.validation_action_button.click()
            self.assertEqual(view._validation_state, DatasetValidationGuiState.VALID)
            self.assertEqual(view.validation_status_label.text(), "Valid dataset/manifest pair")
            summary = view.validation_summary_label.text()
            self.assertIn("Valid records: 1", summary)
            self.assertIn("Nonblank physical lines: 1", summary)
            self.assertIn("Pair comparison actual count: 1", summary)
            self.assertIn("Manifest declared count: 1", summary)
            self.assertIn("Record-count comparison: Match", summary)
            self.assertIn("SHA-256 comparison: Match", summary)
            self.assertIn("Schema/version compatibility: Not reported", summary)

            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
            manifest_data["record_count"] = 2
            manifest_data["sha256"] = "0" * 64
            manifest.write_text(json.dumps(manifest_data), encoding="utf-8")
            view.validation_action_button.click()
            self.assertEqual(view._validation_state, DatasetValidationGuiState.INVALID)
            self.assertIn("record count and SHA-256 differ", view.validation_status_label.text())
            mismatch_summary = view.validation_summary_label.text()
            self.assertIn("Pair comparison actual count: 1", mismatch_summary)
            self.assertIn("Manifest declared count: 2", mismatch_summary)
            self.assertIn("Record-count comparison: Mismatch", mismatch_summary)
            self.assertIn("SHA-256 comparison: Mismatch", mismatch_summary)
            self.assertEqual(view._validation_issue_model.rowCount(), 2)
        finally:
            view.close()

    def test_pair_issue_rows_preserve_nested_then_pair_order(self) -> None:
        dataset = self.root / "bad.jsonl"
        dataset.write_text("{not valid json}\n", encoding="utf-8")
        manifest = self.root / "bad.manifest.json"
        manifest.write_text("{not valid json}", encoding="utf-8")
        view = self._validation_view()
        try:
            self._select_validation_mode(view, DatasetValidationMode.PAIR)
            view.validation_dataset_edit.setText(str(dataset))
            view.validation_manifest_edit.setText(str(manifest))
            view.validation_action_button.click()
            self.assertEqual(view._validation_state, DatasetValidationGuiState.INVALID)
            model = view._validation_issue_model
            sources = [model.data(model.index(row, 0)) for row in range(model.rowCount())]
            self.assertEqual(sources[:2], ["dataset", "manifest"])
            self.assertEqual(sources[2:], ["pair", "pair"])
            codes = [model.data(model.index(row, 1)) for row in range(model.rowCount())]
            self.assertEqual(codes[:2], ["malformed_json", "malformed_json"])
            self.assertEqual(codes[2:], ["dataset_invalid", "manifest_invalid"])
        finally:
            view.close()

    def test_adjacent_manifest_and_clear_preserve_mode_without_writes(self) -> None:
        dataset, manifest = self._standalone_dataset()
        original_settings = (
            self.context.settings.path.read_bytes()
            if self.context.settings.path.exists()
            else None
        )
        view = self._validation_view()
        try:
            self._select_validation_mode(view, DatasetValidationMode.PAIR)
            view.validation_dataset_edit.setText(str(dataset))
            view.use_adjacent_manifest_button.click()
            self.assertEqual(Path(view.validation_manifest_edit.text()), manifest)
            self.assertEqual(view._validation_state, DatasetValidationGuiState.READY)
            view.validation_action_button.click()
            self.assertEqual(view._validation_state, DatasetValidationGuiState.VALID)
            view.validation_clear_button.click()
            self.assertEqual(view._validation_mode, DatasetValidationMode.PAIR)
            self.assertEqual(view.validation_dataset_edit.text(), "")
            self.assertEqual(view.validation_manifest_edit.text(), "")
            self.assertEqual(view._validation_state, DatasetValidationGuiState.EMPTY)
            self.assertEqual(view._validation_issue_model.rowCount(), 0)
            self.assertEqual(
                self.context.settings.path.read_bytes()
                if self.context.settings.path.exists()
                else None,
                original_settings,
            )
        finally:
            view.close()

    def test_missing_file_preserves_path_and_validation_exception_is_recoverable(self) -> None:
        missing = self.root / "missing.jsonl"
        view = self._validation_view()
        try:
            view.validation_dataset_edit.setText(str(missing))
            view.validation_action_button.click()
            self.assertEqual(view._validation_state, DatasetValidationGuiState.RECOVERABLE_FAILURE)
            self.assertEqual(view.validation_dataset_edit.text(), str(missing))
            self.assertIn("could not be read", view.validation_status_label.text().lower())
            self.assertTrue(view.validation_action_button.isEnabled())

            with patch.object(
                self.context.dataset_builder,
                "validate_dataset",
                side_effect=RuntimeError("test failure"),
            ):
                view.validation_action_button.click()
            self.assertEqual(view._validation_state, DatasetValidationGuiState.RECOVERABLE_FAILURE)
            self.assertIn("could not be completed", view.validation_status_label.text().lower())
            self.assertNotIn("Traceback", view.validation_summary_label.text())
            self.assertTrue(view.validation_action_button.isEnabled())
        finally:
            view.close()

    def test_validation_reentry_is_guarded_and_build_state_is_independent(self) -> None:
        dataset, _manifest = self._standalone_dataset()
        view = self._validation_view()
        try:
            view.validation_dataset_edit.setText(str(dataset))
            original_validate = self.context.dataset_builder.validate_dataset

            def reenter(path: Path):
                view.validate_current()
                return original_validate(path)

            with patch.object(
                self.context.dataset_builder,
                "validate_dataset",
                side_effect=reenter,
            ) as validate:
                view.validation_action_button.click()
            self.assertEqual(validate.call_count, 1)
            self.assertEqual(view._validation_state, DatasetValidationGuiState.VALID)
            summary = view.validation_summary_label.text()
            view.refresh_catalog_choices()
            self.assertEqual(view._validation_state, DatasetValidationGuiState.VALID)
            self.assertEqual(view.validation_summary_label.text(), summary)
            self.assertEqual(view._state, DatasetBuilderState.CONFIGURE)
        finally:
            view.close()

    def test_validation_open_failures_do_not_clear_result(self) -> None:
        dataset, _manifest = self._standalone_dataset()
        view = self._validation_view()
        try:
            view.validation_dataset_edit.setText(str(dataset))
            view.validation_action_button.click()
            self.assertTrue(view.validation_open_dataset_button.isEnabled())
            self.assertTrue(view.validation_open_folder_button.isEnabled())
            with patch(
                "gui.views.dataset_builder.QDesktopServices.openUrl",
                return_value=False,
            ), patch("gui.views.dataset_builder.QMessageBox.warning") as warning:
                view.validation_open_dataset_button.click()
                view.validation_open_folder_button.click()
            self.assertEqual(warning.call_count, 2)
            self.assertEqual(view._validation_state, DatasetValidationGuiState.VALID)
            self.assertIn("Valid records: 1", view.validation_summary_label.text())
        finally:
            view.close()

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
