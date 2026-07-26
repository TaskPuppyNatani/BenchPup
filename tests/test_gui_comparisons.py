from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QTableView
from PySide6.QtCore import Qt

from engine.domain import BenchmarkRun, BenchmarkSession, ReviewScore, ScoreboardEntry, ScoreboardImportBatch
from gui.context import GuiApplicationContext
from gui.views.comparisons import ComparisonsView


class GuiComparisonsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-comparisons-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.context = GuiApplicationContext.create(
            database_path=Path(self.directory.name) / "data" / "benchmark.db"
        )

    def tearDown(self) -> None:
        self.context.close()
        self.directory.cleanup()

    def add_run(
        self,
        model: str,
        session_id: int | None = None,
        score: float | None = 4.0,
        benchmark: str = "Shared",
    ) -> int:
        run = BenchmarkRun(
            raw_model_output=f"output-{model}-{benchmark}-{session_id}",
            session_id=session_id,
            model_snapshot={"model_name": model, "tokens_per_second": 100.0},
            benchmark_snapshot={"name": benchmark, "benchmark_type": "code_review"},
            hardware_snapshot={"name": "Rig"},
        )
        saved, _ = self.context.benchmarks.save_run(
            run,
            ReviewScore(run_id=0, overall_score=score, hallucination_level="Low", reliability_level="High"),
        )
        assert saved.id is not None
        return saved.id

    def test_empty_benchmark_model_discovery_has_readable_state_and_disabled_compare(self) -> None:
        view = ComparisonsView(self.context)

        view.refresh()

        self.assertEqual(view.source_selector.currentData(), "benchmark_runs")
        self.assertEqual(view.dimension_selector.currentData(), "models")
        self.assertFalse(view.compare_button.isEnabled())
        self.assertIn("No source data", view.state_banner.text())
        self.assertGreater(view.available_table.model().rowCount(), -1)
        view.deleteLater()

    def test_benchmark_model_discovery_and_ordered_selection_use_real_handlers(self) -> None:
        self.add_run("Beta")
        self.add_run("Alpha")
        view = ComparisonsView(self.context)

        view.refresh()

        self.assertEqual(
            [row.label for row in view.available_model.rows()],
            ["Alpha", "Beta"],
        )
        view.available_table.selectRow(0)
        view.add_subject_button.click()
        view.available_table.selectRow(1)
        view.add_subject_button.click()

        self.assertEqual(view.selected_list.count(), 2)
        self.assertTrue(view.selected_list.item(0).text().startswith("Baseline"))
        self.assertTrue(view.compare_button.isEnabled())
        view.deleteLater()

    def test_scoreboard_model_discovery_is_source_separate(self) -> None:
        self.context.catalog.scoreboard_entries.create(ScoreboardEntry(model_name="Score Alpha", score=4.0))
        self.context.catalog.scoreboard_entries.create(ScoreboardEntry(model_name="Score Beta", score=3.0))
        view = ComparisonsView(self.context)

        view.source_selector.setCurrentIndex(1)

        self.assertEqual(view.dimension_selector.currentData(), "models")
        self.assertFalse(view.dimension_selector.isEnabled())
        self.assertEqual(
            [row.label for row in view.available_model.rows()],
            ["Score Alpha", "Score Beta"],
        )
        view.deleteLater()

    def test_session_catalog_keeps_empty_sessions_selectable(self) -> None:
        first = self.context.catalog.create_session(BenchmarkSession("First"))
        second = self.context.catalog.create_session(BenchmarkSession("Second"))
        self.add_run("Alpha", session_id=first.id)
        view = ComparisonsView(self.context)

        view.dimension_selector.setCurrentIndex(1)

        self.assertEqual(view.available_model.rowCount(), 2)
        self.assertTrue(any("Second" in row.label for row in view.available_model.rows()))
        self.assertTrue(all(row.records is None for row in view.available_model.rows()))
        view.deleteLater()

    def test_invalid_filter_blocks_discovery_and_preserves_input(self) -> None:
        view = ComparisonsView(self.context)
        view.refresh()
        view.benchmark_filters.include_run_ids.setText("not-an-id")

        view.apply_filters_button.click()

        self.assertIn("positive integers", view.filter_error.text())
        self.assertEqual(view.benchmark_filters.include_run_ids.text(), "not-an-id")
        view.deleteLater()

    def _select_first_two(self, view: ComparisonsView) -> None:
        view.available_table.selectRow(0)
        view.add_subject_button.click()
        view.available_table.selectRow(1)
        view.add_subject_button.click()

    def test_benchmark_model_compare_renders_typed_summary_review_and_pairwise(self) -> None:
        self.add_run("Alpha", score=4.0)
        self.add_run("Beta", score=2.0)
        view = ComparisonsView(self.context)
        view.refresh()
        self._select_first_two(view)

        view.compare_button.click()

        self.assertIsNotNone(view.current_result)
        self.assertIn("Summary", [view.results_tabs.tabText(i) for i in range(view.results_tabs.count())])
        self.assertIn("Review", [view.results_tabs.tabText(i) for i in range(view.results_tabs.count())])
        self.assertIn("Pairwise", [view.results_tabs.tabText(i) for i in range(view.results_tabs.count())])
        self.assertEqual(view.results_tabs.tabText(view.results_tabs.currentIndex()), "Summary")
        self.assertIs(view.results_tabs.currentWidget(), view.results_tabs.widget(view.results_tabs.currentIndex()))
        self.assertEqual(view.results_tabs.indexOf(view.results_placeholder), -1)
        summary = view.result_tables["Summary"]
        self.assertEqual(summary.rowCount(), 2)
        self.assertIn("4.00 / 5", summary.data(summary.index(0, 5)))
        pairwise_label = view.pairwise_direction_label
        self.assertIsNotNone(pairwise_label)
        assert pairwise_label is not None
        self.assertIn("second minus first", pairwise_label.text())
        review = view.result_tables["Review"]
        review_metrics = [review.data(review.index(row, 0)) for row in range(review.rowCount())]
        self.assertIn("Hallucination", review_metrics)
        self.assertIn("Reliability", review_metrics)
        view.deleteLater()

    def test_three_subject_compare_explains_pairwise_limit(self) -> None:
        self.add_run("Alpha")
        self.add_run("Beta")
        self.add_run("Gamma")
        view = ComparisonsView(self.context)
        view.refresh()
        self._select_first_two(view)
        view.available_table.selectRow(2)
        view.add_subject_button.click()

        view.compare_button.click()

        assert view.current_result is not None
        self.assertIsNone(view.current_result.pairwise)
        self.assertEqual(view.results_tabs.tabText(view.results_tabs.currentIndex()), "Summary")
        self.assertIn("Pairwise", [view.results_tabs.tabText(i) for i in range(view.results_tabs.count())])
        pairwise = view.result_tables["Pairwise"]
        self.assertEqual(pairwise.rowCount(), 0)
        pairwise_index = next(
            index for index in range(view.results_tabs.count()) if view.results_tabs.tabText(index) == "Pairwise"
        )
        pairwise_page = view.results_tabs.widget(pairwise_index)
        self.assertIsNotNone(pairwise_page)
        assert pairwise_page is not None
        pairwise_text = " ".join(label.text() for label in pairwise_page.findChildren(QLabel))
        self.assertIn("only for two selected subjects", pairwise_text)
        view.deleteLater()

    def test_five_subject_compare_activates_summary_without_pairwise_values(self) -> None:
        for model in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon"):
            self.add_run(model)
        view = ComparisonsView(self.context)
        view.refresh()

        for row in range(5):
            view.available_table.selectRow(row)
            view.add_subject_button.click()

        view.compare_button.click()

        self.assertIsNotNone(view.current_result)
        self.assertEqual(view.results_tabs.tabText(view.results_tabs.currentIndex()), "Summary")
        self.assertIs(view.results_tabs.currentWidget(), view.results_tabs.widget(view.results_tabs.currentIndex()))
        self.assertEqual(view.results_tabs.indexOf(view.results_placeholder), -1)
        self.assertEqual(view.result_tables["Summary"].rowCount(), 5)
        self.assertEqual(view.result_tables["Pairwise"].rowCount(), 0)
        view.deleteLater()

    def test_scoreboard_compare_does_not_render_benchmark_review_or_alignment(self) -> None:
        self.context.catalog.scoreboard_entries.create(
            ScoreboardEntry(
                model_name="Alpha",
                score=600.0,
                hallucination_level="Low",
                consistency="High",
                reliability_score="High",
            )
        )
        self.context.catalog.scoreboard_entries.create(
            ScoreboardEntry(
                model_name="Beta",
                score=2.0,
                hallucination_level="High",
                consistency="Medium",
                reliability_score="Low",
            )
        )
        view = ComparisonsView(self.context)
        view.source_selector.setCurrentIndex(1)
        view.scoreboard_filters.maximum_score.setText("610")
        view.apply_filters_button.click()
        self._select_first_two(view)

        view.compare_button.click()

        tabs = [view.results_tabs.tabText(i) for i in range(view.results_tabs.count())]
        self.assertIn("Summary", tabs)
        self.assertIn("Categories", tabs)
        self.assertIn("Pairwise", tabs)
        self.assertNotIn("Review", tabs)
        self.assertNotIn("Alignment", tabs)
        view.deleteLater()

    def test_scoreboard_summary_renders_batches_and_imported_range(self) -> None:
        batch = self.context.catalog.scoreboard_import_batches.create(
            ScoreboardImportBatch(name="July import", source_file="july.csv")
        )
        self.context.catalog.scoreboard_entries.create(
            ScoreboardEntry(
                model_name="Alpha",
                score=600.0,
                import_batch_id=batch.id,
                imported_at="2026-07-10T09:00:00+00:00",
            )
        )
        self.context.catalog.scoreboard_entries.create(
            ScoreboardEntry(
                model_name="Beta",
                score=580.0,
                import_batch_id=batch.id,
                imported_at="2026-07-11T09:00:00+00:00",
            )
        )
        view = ComparisonsView(self.context)
        view.source_selector.setCurrentIndex(1)
        view.scoreboard_filters.maximum_score.setText("610")
        view.apply_filters_button.click()
        self._select_first_two(view)

        view.compare_button.click()

        summary = view.result_tables["Summary"]
        headers = [
            summary.headerData(column, Qt.Orientation.Horizontal)
            for column in range(summary.columnCount())
        ]
        self.assertIn("Imported range", headers)
        batch_values = [summary.data(summary.index(row, 8)) for row in range(summary.rowCount())]
        imported_ranges = [summary.data(summary.index(row, 9)) for row in range(summary.rowCount())]
        self.assertTrue(any("July import" in value for value in batch_values))
        self.assertTrue(any("2026-07-10" in value for value in imported_ranges))
        self.assertTrue(any("2026-07-11" in value for value in imported_ranges))
        self.assertIn("600.00", summary.data(summary.index(0, 5)))
        self.assertNotIn("/ 5", summary.data(summary.index(0, 5)))
        view.deleteLater()

    def test_session_compare_uses_only_effective_session_filters(self) -> None:
        first = self.context.catalog.create_session(BenchmarkSession("First"))
        second = self.context.catalog.create_session(BenchmarkSession("Second"))
        self.add_run("Alpha", session_id=first.id, score=4.0)
        self.add_run("Beta", session_id=second.id, score=3.0)
        view = ComparisonsView(self.context)
        view.dimension_selector.setCurrentIndex(1)
        view.benchmark_filters.minimum_score.setText("2")
        view.apply_filters_button.click()
        self._select_first_two(view)

        view.compare_button.click()

        self.assertIsNotNone(view.current_result)
        assert view.current_result is not None
        self.assertEqual(view.current_result.filters.model, "")
        self.assertEqual(view.current_result.filters.session, "")
        self.assertIsNone(view.current_result.filters.session_id)
        self.assertNotIn("session", view.active_filter_summary.text().casefold())
        view.deleteLater()

    def test_missing_values_render_typed_state_and_warning_without_zero_filling(self) -> None:
        self.add_run("Alpha", score=None)
        self.add_run("Beta", score=3.0)
        view = ComparisonsView(self.context)
        view.refresh()
        self._select_first_two(view)

        view.compare_button.click()

        self.assertIn("missing values", view.state_banner.text().casefold())
        summary = view.result_tables["Summary"]
        self.assertEqual(summary.data(summary.index(0, 5)), "Not recorded")
        self.assertGreater(view.warning_list.count(), 0)
        view.deleteLater()

    def test_selection_order_controls_update_baseline_and_clear_results(self) -> None:
        self.add_run("Alpha")
        self.add_run("Beta")
        self.add_run("Gamma")
        view = ComparisonsView(self.context)
        view.refresh()
        self._select_first_two(view)
        view.compare_button.click()
        self.assertIn("Summary", [view.results_tabs.tabText(i) for i in range(view.results_tabs.count())])

        view.available_table.selectRow(2)
        view.add_subject_button.click()
        view.selected_list.setCurrentRow(2)
        view.move_up_button.click()
        view.move_up_button.click()

        self.assertTrue(view.selected_list.item(0).text().startswith("Baseline"))
        self.assertEqual(view.selected_list.count(), 3)
        self.assertEqual(view.results_tabs.tabText(0), "Results")
        view.clear_selection_button.click()
        self.assertEqual(view.selected_list.count(), 0)
        self.assertFalse(view.compare_button.isEnabled())
        view.deleteLater()

    def test_invalidation_restores_one_active_placeholder_after_success(self) -> None:
        self.add_run("Alpha")
        self.add_run("Beta")
        self.add_run("Gamma")
        view = ComparisonsView(self.context)
        view.refresh()
        self._select_first_two(view)
        view.compare_button.click()
        self.assertEqual(view.results_tabs.tabText(view.results_tabs.currentIndex()), "Summary")

        view.available_table.selectRow(2)
        view.add_subject_button.click()

        self.assertEqual(view.results_tabs.count(), 1)
        self.assertEqual(view.results_tabs.indexOf(view.results_placeholder), 0)
        self.assertEqual(view.results_tabs.currentIndex(), 0)
        self.assertIs(view.results_tabs.currentWidget(), view.results_placeholder)
        self.assertIn("Select at least two subjects", view.results_placeholder.text())

        view.refresh()
        self.assertEqual(view.results_tabs.count(), 1)
        self.assertEqual(view.results_tabs.indexOf(view.results_placeholder), 0)
        self.assertEqual(view.results_tabs.currentIndex(), 0)
        view.deleteLater()

    def test_refresh_preserves_selected_identity_order_and_result_is_snapshot_safe(self) -> None:
        first_id = self.add_run("Alpha")
        self.add_run("Beta")
        view = ComparisonsView(self.context)
        view.refresh()
        self._select_first_two(view)
        before = self.context.benchmarks.get_run(first_id)
        assert before[0] is not None
        before_snapshot = dict(before[0].model_snapshot)

        view.refresh()

        self.assertEqual(view.selected_list.count(), 2)
        self.assertTrue(view.selected_list.item(0).text().startswith("Baseline"))
        view.compare_button.click()
        after = self.context.benchmarks.get_run(first_id)
        assert after[0] is not None
        self.assertEqual(after[0].model_snapshot, before_snapshot)
        view.deleteLater()

    def test_source_switch_clears_selection_results_and_disables_scoreboard_dimension(self) -> None:
        self.add_run("Alpha")
        self.add_run("Beta")
        view = ComparisonsView(self.context)
        view.refresh()
        self._select_first_two(view)
        view.compare_button.click()
        self.assertIsNotNone(view.current_result)

        view.source_selector.setCurrentIndex(1)

        self.assertIsNone(view.current_result)
        self.assertEqual(view.selected_list.count(), 0)
        self.assertFalse(view.dimension_selector.isEnabled())
        self.assertEqual(view.results_tabs.tabText(0), "Results")
        view.deleteLater()

    def test_applying_filter_keeps_selected_identity_visible_when_unavailable(self) -> None:
        self.add_run("Alpha", score=2.0)
        self.add_run("Beta", score=4.0)
        view = ComparisonsView(self.context)
        view.refresh()
        self._select_first_two(view)
        view.benchmark_filters.minimum_score.setText("5")
        view.apply_filters_button.click()

        self.assertEqual(view.selected_list.count(), 2)
        self.assertIn("unavailable", view.selected_list.item(0).text().casefold())
        self.assertIn("No records match filters", view.state_banner.text())
        view.deleteLater()

    def test_score_and_date_validation_preserves_inputs_without_service_call(self) -> None:
        view = ComparisonsView(self.context)
        view.refresh()
        view.benchmark_filters.minimum_score.setText("4")
        view.benchmark_filters.maximum_score.setText("2")

        view.apply_filters_button.click()

        self.assertIn("cannot exceed", view.filter_error.text())
        self.assertEqual(view.benchmark_filters.minimum_score.text(), "4")
        self.assertEqual(view.benchmark_filters.maximum_score.text(), "2")
        view.deleteLater()

    def test_repeated_compare_replaces_result_without_accumulating_tabs_or_warnings(self) -> None:
        self.add_run("Alpha", score=None)
        self.add_run("Beta", score=3.0)
        view = ComparisonsView(self.context)
        view.refresh()
        self._select_first_two(view)

        view.compare_button.click()
        first_tabs = [view.results_tabs.tabText(i) for i in range(view.results_tabs.count())]
        first_warnings = view.warning_list.count()
        self.assertEqual(view.results_tabs.tabText(view.results_tabs.currentIndex()), "Summary")
        self.assertEqual(view.results_tabs.indexOf(view.results_placeholder), -1)
        view.compare_button.click()

        self.assertEqual([view.results_tabs.tabText(i) for i in range(view.results_tabs.count())], first_tabs)
        self.assertEqual(view.warning_list.count(), first_warnings)
        self.assertEqual(view.results_tabs.tabText(view.results_tabs.currentIndex()), "Summary")
        self.assertEqual(view.results_tabs.indexOf(view.results_placeholder), -1)
        view.deleteLater()

    def test_model_alignment_renders_shared_non_overlapping_and_excluded_data(self) -> None:
        self.add_run("Alpha", benchmark="Shared")
        self.add_run("Alpha", benchmark="Alpha only")
        self.add_run("Beta", benchmark="Shared")
        self.add_run("Beta", benchmark="Beta only")
        view = ComparisonsView(self.context)
        view.refresh()
        self._select_first_two(view)

        view.compare_button.click()

        self.assertIsNotNone(view.current_result)
        alignment = view.result_tables["Alignment"]
        values = " ".join(
            str(alignment.data(alignment.index(row, column)))
            for row in range(alignment.rowCount())
            for column in range(alignment.columnCount())
        )
        self.assertIn("Shared", values)
        self.assertIn("Alpha only", values)
        self.assertIn("Beta only", values)
        self.assertIn("Excluded benchmark count", values)
        view.deleteLater()

    def test_session_alignment_renders_shared_and_non_overlapping_subject_associations(self) -> None:
        first = self.context.catalog.create_session(BenchmarkSession("First"))
        second = self.context.catalog.create_session(BenchmarkSession("Second"))
        self.add_run("Alpha", session_id=first.id, benchmark="Shared")
        self.add_run("Alpha", session_id=second.id, benchmark="Shared")
        self.add_run("Alpha", session_id=first.id, benchmark="First only")
        self.add_run("Beta", session_id=second.id, benchmark="Second only")
        view = ComparisonsView(self.context)
        view.dimension_selector.setCurrentIndex(1)
        self._select_first_two(view)

        view.compare_button.click()

        self.assertIsNotNone(view.current_result)
        alignment = view.result_tables["Alignment"]
        values = " ".join(
            str(alignment.data(alignment.index(row, column)))
            for row in range(alignment.rowCount())
            for column in range(alignment.columnCount())
        )
        self.assertIn("Shared", values)
        self.assertIn("First only", values)
        self.assertIn("Second only", values)
        self.assertIn("Beta", values)
        self.assertIn("Alpha / First only", values)
        view.deleteLater()

    def test_comparison_failure_clears_old_result_state_and_preserves_configuration(self) -> None:
        self.add_run("Alpha")
        self.add_run("Beta")
        view = ComparisonsView(self.context)
        view.benchmark_filters.minimum_score.setText("1")
        view.apply_filters_button.click()
        self._select_first_two(view)
        view.compare_button.click()
        self.assertIsNotNone(view.current_result)
        self.assertIn("minimum score", view.active_filter_summary.text().casefold())
        selected_labels = [view.selected_list.item(row).text() for row in range(view.selected_list.count())]

        with patch.object(
            view.context.comparisons,
            "compare_benchmark_models",
            side_effect=RuntimeError("simulated comparison failure"),
        ):
            view.compare_button.click()

        self.assertIsNone(view.current_result)
        self.assertEqual(view.results_tabs.count(), 1)
        self.assertEqual(view.results_tabs.tabText(0), "Results")
        self.assertEqual(view.results_tabs.currentIndex(), 0)
        self.assertIs(view.results_tabs.currentWidget(), view.results_placeholder)
        self.assertIn("could not be completed", view.results_placeholder.text().casefold())
        self.assertEqual(view.warning_list.count(), 0)
        self.assertEqual(view.active_filter_summary.text(), "Active filters: none")
        self.assertIn("could not be completed", view.state_banner.text().casefold())
        self.assertEqual(
            [view.selected_list.item(row).text() for row in range(view.selected_list.count())],
            selected_labels,
        )
        self.assertEqual(view.benchmark_filters.minimum_score.text(), "1")
        self.assertTrue(view.compare_button.isEnabled())
        view.deleteLater()

    def test_refresh_clears_result_state_and_discovers_once(self) -> None:
        self.add_run("Alpha")
        self.add_run("Beta")
        view = ComparisonsView(self.context)
        view.refresh()
        self._select_first_two(view)
        view.compare_button.click()
        self.assertIsNotNone(view.current_result)

        original = view.context.comparisons.discover_benchmark_model_subjects
        with patch.object(
            view.context.comparisons,
            "discover_benchmark_model_subjects",
            wraps=original,
        ) as discover:
            view.refresh()

        self.assertEqual(discover.call_count, 1)
        self.assertIsNone(view.current_result)
        self.assertEqual(view.results_tabs.tabText(0), "Results")
        self.assertEqual(view.warning_list.count(), 0)
        self.assertEqual(view.active_filter_summary.text(), "Active filters: none")
        self.assertEqual(view.selected_list.count(), 2)
        self.assertTrue(view.selected_list.item(0).text().startswith("Baseline"))
        view.deleteLater()

    def test_correcting_filter_input_clears_obsolete_validation_error(self) -> None:
        view = ComparisonsView(self.context)
        view.refresh()
        view.benchmark_filters.include_run_ids.setText("not-an-id")
        view.apply_filters_button.click()
        self.assertIn("positive integers", view.filter_error.text())

        view.benchmark_filters.include_run_ids.setText("1")

        self.assertEqual(view.filter_error.text(), "")
        view.deleteLater()

    def test_unknown_result_state_uses_safe_fallback_copy(self) -> None:
        view = ComparisonsView(self.context)

        view._render_state(object())

        self.assertIn("Comparison status", view.state_banner.text())
        self.assertTrue(view.state_banner.toolTip())
        view.deleteLater()

    def test_result_pages_and_models_are_replaced_without_registry_or_tab_growth(self) -> None:
        self.add_run("Alpha")
        self.add_run("Beta")
        view = ComparisonsView(self.context)
        view.refresh()
        self._select_first_two(view)
        view.compare_button.click()
        initial_tabs = [view.results_tabs.tabText(index) for index in range(view.results_tabs.count())]
        initial_registry_keys = set(view.result_tables)
        old_page = view.results_tabs.widget(1)
        old_models = tuple(view.result_tables.values())

        view.compare_button.click()

        self.assertEqual(
            [view.results_tabs.tabText(index) for index in range(view.results_tabs.count())],
            initial_tabs,
        )
        self.assertEqual(set(view.result_tables), initial_registry_keys)
        self.assertEqual(view.results_tabs.indexOf(old_page), -1)
        self.assertTrue(all(model not in view.result_tables.values() for model in old_models))

        for _ in range(3):
            view.compare_button.click()
            self.assertEqual(view.results_tabs.count(), len(initial_tabs))
            self.assertEqual(set(view.result_tables), initial_registry_keys)

        original = view.context.comparisons.compare_benchmark_models
        with patch.object(view.context.comparisons, "compare_benchmark_models", wraps=original) as compare:
            view.compare_button.click()
        self.assertEqual(compare.call_count, 1)
        view.deleteLater()

    def test_pairwise_direction_label_is_cleared_and_recreated_on_real_replacement(self) -> None:
        self.add_run("Alpha")
        self.add_run("Beta")
        view = ComparisonsView(self.context)
        view.refresh()
        self._select_first_two(view)

        view.compare_button.click()

        old_label = view.pairwise_direction_label
        self.assertIsNotNone(old_label)

        view.refresh()

        self.assertIsNone(view.pairwise_direction_label)

        view.compare_button.click()

        new_label = view.pairwise_direction_label
        self.assertIsNotNone(new_label)
        self.assertIsNot(old_label, new_label)
        self.assertIn("second minus first", new_label.text())
        view.deleteLater()

    def test_result_page_native_cleanup_exits_normally_in_isolated_process(self) -> None:
        script = """
import os
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from engine.domain import BenchmarkRun, ReviewScore
from gui.context import GuiApplicationContext
from gui.views.comparisons import ComparisonsView


app = QApplication(["benchpup-comparison-cleanup"])
with tempfile.TemporaryDirectory() as temporary_directory:
    context = GuiApplicationContext.create(
        database_path=Path(temporary_directory) / "data" / "benchmark.db"
    )
    try:
        for model in ("Alpha", "Beta"):
            run = BenchmarkRun(
                raw_model_output=f"output-{model}",
                model_snapshot={"model_name": model, "tokens_per_second": 100.0},
                benchmark_snapshot={"name": "Shared", "benchmark_type": "code_review"},
                hardware_snapshot={"name": "Rig"},
            )
            context.benchmarks.save_run(
                run,
                ReviewScore(
                    run_id=0,
                    overall_score=4.0,
                    hallucination_level="Low",
                    reliability_level="High",
                ),
            )

        view = ComparisonsView(context)
        view.refresh()
        view.available_table.selectRow(0)
        view.add_subject_button.click()
        view.available_table.selectRow(1)
        view.add_subject_button.click()
        if not view.compare_button.isEnabled():
            raise RuntimeError("real comparison controls did not become enabled")

        view.compare_button.click()
        if view.current_result is None or view.pairwise_direction_label is None:
            raise RuntimeError("first real comparison did not render pairwise results")
        if "Pairwise" not in [view.results_tabs.tabText(index) for index in range(view.results_tabs.count())]:
            raise RuntimeError("first real comparison did not render a pairwise page")
        old_page = view.results_tabs.widget(1)
        old_model_ids = {id(model) for model in view.result_tables.values()}

        view.compare_button.click()
        if view.current_result is None:
            raise RuntimeError("second real comparison did not render a result")
        if view.results_tabs.indexOf(old_page) != -1:
            raise RuntimeError("old result page remained active after replacement")
        if any(id(model) in old_model_ids for model in view.result_tables.values()):
            raise RuntimeError("old result model remained active after replacement")
        view.deleteLater()
    finally:
        context.close()

QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
app.processEvents()
print("isolated cleanup complete")
"""
        environment = os.environ.copy()
        environment["QT_QPA_PLATFORM"] = "offscreen"
        source_path = str(Path(__file__).parents[1] / "src")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (source_path, environment.get("PYTHONPATH", "")) if value
        )
        completed = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-c", script],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )
        self.assertIn("isolated cleanup complete", completed.stdout)

    def test_duplicate_session_titles_keep_distinct_typed_selection_order(self) -> None:
        first = self.context.catalog.create_session(BenchmarkSession("Same title"))
        second = self.context.catalog.create_session(BenchmarkSession("Same title"))
        self.add_run("Alpha", session_id=first.id)
        self.add_run("Beta", session_id=second.id)
        view = ComparisonsView(self.context)
        view.dimension_selector.setCurrentIndex(1)
        labels = [row.label for row in view.available_model.rows()]
        self.assertEqual(len(set(labels)), 2)
        self.assertTrue(all("#" in label for label in labels))
        self._select_first_two(view)

        original = view.context.comparisons.compare_sessions
        with patch.object(view.context.comparisons, "compare_sessions", wraps=original) as compare:
            view.compare_button.click()

        selected_sessions = compare.call_args.args[0]
        self.assertEqual([session.id for session in selected_sessions], [first.id, second.id])
        self.assertIsNotNone(view.current_result)
        view.deleteLater()

    def test_selection_changes_do_not_rediscover_subjects(self) -> None:
        self.add_run("Alpha")
        self.add_run("Beta")
        view = ComparisonsView(self.context)
        view.refresh()
        original = view.context.comparisons.discover_benchmark_model_subjects
        with patch.object(
            view.context.comparisons,
            "discover_benchmark_model_subjects",
            wraps=original,
        ) as discover:
            self._select_first_two(view)
            view.selected_list.setCurrentRow(1)
            view.remove_subject_button.click()

        self.assertEqual(discover.call_count, 0)
        view.deleteLater()

    def test_comparison_controls_are_accessible_and_read_only_actions_do_not_write(self) -> None:
        self.add_run("Alpha")
        self.add_run("Beta")
        before_database = self.context.paths.database_path.read_bytes()
        view = ComparisonsView(self.context)
        view.refresh()
        self._select_first_two(view)
        view.compare_button.click()

        for widget in (
            view.add_subject_button,
            view.remove_subject_button,
            view.move_up_button,
            view.move_down_button,
            view.clear_selection_button,
            view.source_selector,
            view.dimension_selector,
            view.refresh_subjects_button,
            view.apply_filters_button,
            view.clear_filters_button,
            view.compare_button,
            view.available_search,
            view.available_table,
            view.selected_list,
            view.state_banner,
            view.active_filter_summary,
            view.warning_list,
            view.results_tabs,
        ):
            self.assertTrue(widget.accessibleName(), widget.objectName())
        summary_index = next(
            index
            for index in range(view.results_tabs.count())
            if view.results_tabs.tabText(index) == "Summary"
        )
        summary_table = view.results_tabs.widget(summary_index).findChild(QTableView)
        self.assertIsNotNone(summary_table)
        assert summary_table is not None
        self.assertIn("summary", summary_table.accessibleName().casefold())
        self.assertNotEqual(summary_table.focusPolicy(), Qt.FocusPolicy.NoFocus)
        summary_values = " ".join(
            str(view.result_tables["Summary"].data(view.result_tables["Summary"].index(row, column)))
            for row in range(view.result_tables["Summary"].rowCount())
            for column in range(view.result_tables["Summary"].columnCount())
        )
        self.assertIn("Baseline", summary_values)
        self.assertTrue(view.state_banner.text())
        self.assertTrue(view.compare_button.hasFocus() or view.compare_button.focusPolicy() != Qt.FocusPolicy.NoFocus)
        self.assertEqual(self.context.paths.database_path.read_bytes(), before_database)
        view.deleteLater()

    def test_comparisons_normal_window_scrolls_and_preserves_control_heights(self) -> None:
        view = ComparisonsView(self.context)
        view.resize(900, 560)
        view.show()
        self.application.processEvents()

        self.assertIsInstance(view.content_scroll, QScrollArea)
        self.assertTrue(view.content_scroll.widgetResizable())
        self.assertEqual(
            view.content_scroll.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self.assertEqual(
            view.content_scroll.verticalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        )
        self.assertTrue(view.content_scroll.accessibleName())
        self.assertGreater(view.content_scroll.verticalScrollBar().maximum(), 0)

        controls = (
            view.source_selector,
            view.dimension_selector,
            view.benchmark_filters.benchmark_edit,
            view.benchmark_filters.benchmark_type,
            view.benchmark_filters.session_edit,
            view.benchmark_filters.session_id,
            view.benchmark_filters.hardware_edit,
            view.benchmark_filters.hardware_profile_id,
            view.benchmark_filters.minimum_score,
            view.benchmark_filters.maximum_score,
            view.benchmark_filters.hallucination,
            view.benchmark_filters.reliability,
            view.benchmark_filters.include_run_ids,
            view.benchmark_filters.exclude_run_ids,
            view.benchmark_filters.date_range.from_check,
            view.benchmark_filters.date_range.from_date,
            view.benchmark_filters.date_range.to_check,
            view.benchmark_filters.date_range.to_date,
            view.scoreboard_filters.batch_id,
            view.scoreboard_filters.minimum_score,
            view.scoreboard_filters.maximum_score,
            view.scoreboard_filters.hallucination,
            view.scoreboard_filters.consistency,
            view.scoreboard_filters.reliability,
            view.scoreboard_filters.include_deleted,
            view.available_search,
            view.refresh_subjects_button,
            view.apply_filters_button,
            view.clear_filters_button,
            view.add_subject_button,
            view.remove_subject_button,
            view.move_up_button,
            view.move_down_button,
            view.clear_selection_button,
            view.compare_button,
        )
        for control in controls:
            expected_minimum = max(
                30,
                control.sizeHint().height(),
                control.minimumSizeHint().height(),
            )
            self.assertGreaterEqual(control.minimumHeight(), expected_minimum, control.objectName())
        self.assertGreaterEqual(view.selection_splitter.minimumHeight(), 180)
        self.assertGreaterEqual(view.available_table.height(), 120)
        self.assertGreaterEqual(view.selected_list.height(), 120)
        view.deleteLater()

    def test_comparisons_large_window_expands_without_horizontal_scroll(self) -> None:
        view = ComparisonsView(self.context)
        view.resize(1400, 1000)
        view.show()
        self.application.processEvents()

        self.assertEqual(view.content_scroll.horizontalScrollBar().maximum(), 0)
        self.assertGreaterEqual(view.content_scroll.viewport().height(), 800)
        self.assertGreaterEqual(view.content_scroll.viewport().width(), 1000)
        for control in (
            view.source_selector,
            view.dimension_selector,
            view.available_search,
            view.refresh_subjects_button,
            view.compare_button,
        ):
            self.assertLessEqual(control.height(), control.minimumHeight() + 4, control.objectName())
        self.assertGreater(view.selection_splitter.width(), 0)
        view.deleteLater()

    def test_comparisons_controls_remain_keyboard_reachable_inside_scroll_area(self) -> None:
        view = ComparisonsView(self.context)
        view.resize(900, 560)
        view.show()
        self.application.processEvents()

        for control in (
            view.source_selector,
            view.dimension_selector,
            view.benchmark_filters.benchmark_edit,
            view.apply_filters_button,
            view.available_table,
            view.add_subject_button,
            view.selected_list,
            view.compare_button,
        ):
            self.assertNotEqual(control.focusPolicy(), Qt.FocusPolicy.NoFocus, control.objectName())
        view.deleteLater()


if __name__ == "__main__":
    unittest.main()
