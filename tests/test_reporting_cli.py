import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cli import (
    BenchmarkReportOptions,
    HardwareReportOptions,
    LeaderboardReportOptions,
    QuitApplication,
    ScoreboardReportOptions,
    SessionReportOptions,
    TerminalApp,
)
from engine.domain import (
    BenchmarkDefinition,
    BenchmarkRun,
    BenchmarkSession,
    HardwareProfile,
    ModelProfile,
    ReviewScore,
    ScoreboardEntry,
    ScoreboardImportBatch,
)
from engine.reporting import (
    BenchmarkReportFilters,
    BenchmarkRunAggregate,
    ReportWriteResult,
    ReportWriteStatus,
    ReportingService,
    build_benchmark_run_report,
    build_hardware_report,
    build_model_leaderboard,
    build_scoreboard_report,
    build_session_report,
)


class ReportingCliTests(unittest.TestCase):
    def app_with(self, answers: list[str]) -> tuple[TerminalApp, list[str]]:
        directory = tempfile.TemporaryDirectory()
        output: list[str] = []
        iterator = iter(answers)
        app = TerminalApp(
            Path(directory.name) / "benchmarks.db",
            input_fn=lambda _: next(iterator),
            output_fn=output.append,
        )
        self.addCleanup(directory.cleanup)
        return app, output

    @staticmethod
    def run_aggregate(model_name: str = "Alpha", score: float | None = 4.5) -> BenchmarkRunAggregate:
        run = BenchmarkRun(
            raw_model_output="raw output",
            prompt_text="prompt text",
            model_snapshot={"model_name": model_name, "tokens_per_second": 100.0},
            benchmark_snapshot={"name": "Review", "file_path": "review.py", "benchmark_type": "code_review"},
            prompt_snapshot={"name": "Prompt", "version": "1", "prompt_text": "prompt text"},
            created_at="2026-07-18T00:00:00+00:00",
        )
        review = ReviewScore(run_id=1, overall_score=score) if score is not None else None
        return BenchmarkRunAggregate(run, review)

    def report_fixture(self):
        return build_benchmark_run_report([self.run_aggregate()])

    def test_reporting_menu_navigation_back_and_qa(self) -> None:
        app, output = self.app_with(["b"])
        app.reporting_screen()
        self.assertIn("1) Detailed Benchmark Run Report", "\n".join(output))
        self.assertIn("4) Session Report", "\n".join(output))
        self.assertIn("5) Hardware Report", "\n".join(output))
        self.assertIn("6) View Current Report Options", "\n".join(output))

        app, _ = self.app_with(["qa"])
        with self.assertRaises(QuitApplication):
            app.reporting_screen()

    def test_main_menu_exposes_reports_screen(self) -> None:
        app, output = self.app_with(["q"])
        app.run()
        self.assertIn("17) Reports", "\n".join(output))

    def test_detailed_report_uses_service_and_keeps_optional_content_excluded(self) -> None:
        app, _ = self.app_with(["3", "y", "b", "b"])
        report = self.report_fixture()
        app.reporting = Mock(spec=ReportingService)
        app.reporting.benchmark_run_report.return_value = report
        app.reporting.write_markdown_report.return_value = ReportWriteResult(
            ReportWriteStatus.SUCCESS,
            Path("report.md"),
            "written",
        )
        destination = Path(app.benchmarks.database.path.parent) / "report.md"
        options = BenchmarkReportOptions()
        with patch.object(app, "prompt_path", return_value=str(destination)), patch.object(
            app, "prepare_export_destination", return_value=destination
        ):
            app._detailed_benchmark_report_screen(options)

        app.reporting.benchmark_run_report.assert_called_once_with(
            filters=options.filters,
            include_prompt_text=False,
            include_raw_model_output=False,
            include_attachment_metadata=False,
            title=options.title,
        )
        app.reporting.write_markdown_report.assert_called_once()
        write_kwargs = app.reporting.write_markdown_report.call_args.kwargs
        self.assertFalse(write_kwargs["include_model_details"])

    def test_detailed_report_propagates_filters_and_optional_content(self) -> None:
        app, _ = self.app_with(["3", "y", "b", "b"])
        report = self.report_fixture()
        app.reporting = Mock(spec=ReportingService)
        app.reporting.benchmark_run_report.return_value = report
        app.reporting.write_markdown_report.return_value = ReportWriteResult(ReportWriteStatus.SUCCESS, Path("report.md"))
        destination = Path(app.benchmarks.database.path.parent) / "report.md"
        from engine.reporting import BenchmarkReportFilters

        options = BenchmarkReportOptions(
            title="Filtered Report",
            filters=BenchmarkReportFilters(benchmark_type="code_review", session_id=7, hardware="RTX"),
            include_prompt_text=True,
            include_raw_model_output=True,
            include_attachment_metadata=True,
        )
        with patch.object(app, "prompt_path", return_value=str(destination)), patch.object(
            app, "prepare_export_destination", return_value=destination
        ):
            app._detailed_benchmark_report_screen(options)

        app.reporting.benchmark_run_report.assert_called_once_with(
            filters=options.filters,
            include_prompt_text=True,
            include_raw_model_output=True,
            include_attachment_metadata=True,
            title="Filtered Report",
        )

    def test_real_reporting_service_writes_utf8_report_with_default_content_exclusions(self) -> None:
        app, _ = self.app_with(["3", "y", "b", "b"])
        app.benchmarks.save_run(
            BenchmarkRun(
                raw_model_output="private raw output",
                model_snapshot={"model_name": "Alpha"},
                benchmark_snapshot={"name": "Review", "benchmark_type": "code_review"},
                prompt_snapshot={"prompt_text": "private prompt"},
                prompt_text="private prompt",
            ),
            ReviewScore(run_id=0, overall_score=4.0),
        )
        destination = app.benchmarks.database.path.parent / "report.md"

        with patch.object(app, "prompt_path", return_value=str(destination)):
            app._detailed_benchmark_report_screen(BenchmarkReportOptions())

        self.assertTrue(destination.exists())
        rendered = destination.read_text(encoding="utf-8")
        self.assertIn("# Benchmark Run Report", rendered)
        self.assertNotIn("private raw output", rendered)
        self.assertNotIn("private prompt", rendered)

    def test_report_cancellation_writes_nothing(self) -> None:
        app, _ = self.app_with(["3", "n", "b"])
        app.reporting = Mock(spec=ReportingService)
        app.reporting.benchmark_run_report.return_value = self.report_fixture()
        app.reporting.write_markdown_report.return_value = ReportWriteResult(ReportWriteStatus.SUCCESS, Path("report.md"))
        destination = Path(app.benchmarks.database.path.parent) / "report.md"
        with patch.object(app, "prompt_path", return_value=str(destination)), patch.object(
            app, "prepare_export_destination", return_value=destination
        ):
            app._detailed_benchmark_report_screen(BenchmarkReportOptions())
        app.reporting.write_markdown_report.assert_not_called()

    def test_overwrite_requires_explicit_confirmation_and_can_succeed(self) -> None:
        app, _ = self.app_with(["3", "y", "y", "b", "b"])
        app.reporting = Mock(spec=ReportingService)
        app.reporting.benchmark_run_report.return_value = self.report_fixture()
        destination = Path(app.benchmarks.database.path.parent) / "report.md"
        app.reporting.write_markdown_report.side_effect = [
            ReportWriteResult(ReportWriteStatus.OVERWRITE_REQUIRED, destination, "existing"),
            ReportWriteResult(ReportWriteStatus.SUCCESS, destination, "written"),
        ]
        with patch.object(app, "prompt_path", return_value=str(destination)), patch.object(
            app, "prepare_export_destination", return_value=destination
        ):
            app._detailed_benchmark_report_screen(BenchmarkReportOptions())
        self.assertEqual(app.reporting.write_markdown_report.call_count, 2)
        self.assertTrue(app.reporting.write_markdown_report.call_args_list[1].kwargs["overwrite"])

    def test_overwrite_declined_does_not_retry(self) -> None:
        app, output = self.app_with(["3", "y", "n", "b", "b"])
        app.reporting = Mock(spec=ReportingService)
        app.reporting.benchmark_run_report.return_value = self.report_fixture()
        destination = Path(app.benchmarks.database.path.parent) / "report.md"
        app.reporting.write_markdown_report.return_value = ReportWriteResult(
            ReportWriteStatus.OVERWRITE_REQUIRED,
            destination,
            "existing",
        )
        with patch.object(app, "prompt_path", return_value=str(destination)), patch.object(
            app, "prepare_export_destination", return_value=destination
        ):
            app._detailed_benchmark_report_screen(BenchmarkReportOptions())
        app.reporting.write_markdown_report.assert_called_once()
        self.assertIn("no replacement was written", "\n".join(output).lower())

    def test_structured_write_failure_is_friendly(self) -> None:
        app, output = self.app_with(["3", "y", "b", "b"])
        app.reporting = Mock(spec=ReportingService)
        app.reporting.benchmark_run_report.return_value = self.report_fixture()
        app.reporting.write_markdown_report.return_value = ReportWriteResult(
            ReportWriteStatus.FINALIZE_FAILED,
            Path("report.md"),
            "Could not finalize",
        )
        destination = Path(app.benchmarks.database.path.parent) / "report.md"
        with patch.object(app, "prompt_path", return_value=str(destination)), patch.object(
            app, "prepare_export_destination", return_value=destination
        ):
            app._detailed_benchmark_report_screen(BenchmarkReportOptions())
        rendered = "\n".join(output)
        self.assertIn("Report Finalization Failed", rendered)
        self.assertNotIn("Traceback", rendered)

    def test_filter_catalog_choices_are_vertical_and_propagate_without_manual_ids(self) -> None:
        app, _ = self.app_with(["1", "1", "2", "1", "4", "1", "5", "1", "b"])
        session = app.catalog.sessions.create(BenchmarkSession(title="July session"))
        app.catalog.model_profiles.create(ModelProfile(name="Alpha profile", model_name="Alpha"))
        app.catalog.benchmark_definitions.create(
            BenchmarkDefinition(name="Review", file_path="review.py", benchmark_type="code_review")
        )
        app.catalog.hardware_profiles.create(HardwareProfile(name="Rig A"))

        from engine.reporting import BenchmarkReportFilters

        filters = app._report_filters_screen(BenchmarkReportFilters())

        self.assertEqual(filters.benchmark_type, "code_review")
        self.assertEqual(filters.benchmark, "Review")
        self.assertEqual(filters.session_id, session.id)
        self.assertEqual(filters.hardware_profile_id, app.catalog.hardware_profiles.list()[0].id)

    def test_scoreboard_report_uses_batch_filter_and_service(self) -> None:
        app, _ = self.app_with(["3", "y", "b", "b"])
        batch = ScoreboardImportBatch(name="July", source_file="july.csv", id=10)
        entry = ScoreboardEntry(model_name="Alpha", score=4.0, import_batch_id=10)
        report = build_scoreboard_report([entry], batches=[batch])
        app.reporting = Mock(spec=ReportingService)
        app.reporting.scoreboard_report.return_value = report
        app.reporting.write_markdown_report.return_value = ReportWriteResult(ReportWriteStatus.SUCCESS, Path("scoreboard.md"))
        destination = Path(app.benchmarks.database.path.parent) / "scoreboard.md"
        options = ScoreboardReportOptions()
        with patch.object(app, "prompt_path", return_value=str(destination)), patch.object(
            app, "prepare_export_destination", return_value=destination
        ):
            app._historical_scoreboard_report_screen(options)
        app.reporting.scoreboard_report.assert_called_once_with(filters=options.filters, title=options.title)

    def test_empty_benchmark_and_leaderboard_selection_does_not_write(self) -> None:
        app, output = self.app_with(["3", "b", "b"])
        empty_report = build_benchmark_run_report([])
        app.reporting = Mock(spec=ReportingService)
        app.reporting.benchmark_run_report.return_value = empty_report
        app._detailed_benchmark_report_screen(BenchmarkReportOptions())
        self.assertIn("No benchmark runs match", "\n".join(output))
        app.reporting.write_markdown_report.assert_not_called()

        app, output = self.app_with(["4", "b", "b"])
        no_score_report = build_model_leaderboard([self.run_aggregate(score=None)])
        app.reporting = Mock(spec=ReportingService)
        app.reporting.model_leaderboard.return_value = no_score_report
        app._model_leaderboard_report_screen(LeaderboardReportOptions())
        self.assertIn("No eligible scored runs", "\n".join(output))
        app.reporting.write_markdown_report.assert_not_called()

    def test_leaderboard_terminal_preview_uses_structured_engine_order(self) -> None:
        app, output = self.app_with(["3", "b", "b"])
        first = self.run_aggregate("First", 4.0)
        second = self.run_aggregate("Second", 5.0)
        report = build_model_leaderboard([first, second])
        app.reporting = Mock(spec=ReportingService)
        app.reporting.model_leaderboard.return_value = report
        app._model_leaderboard_report_screen(LeaderboardReportOptions())
        rendered = "\n".join(output)
        self.assertIn("1) Second | average=5", rendered)
        self.assertIn("2) First | average=4", rendered)
        app.reporting.model_leaderboard.assert_called_once()

    def test_leaderboard_write_propagates_detail_option(self) -> None:
        app, _ = self.app_with(["4", "y", "b", "b"])
        report = build_model_leaderboard([self.run_aggregate()])
        app.reporting = Mock(spec=ReportingService)
        app.reporting.model_leaderboard.return_value = report
        app.reporting.write_markdown_report.return_value = ReportWriteResult(ReportWriteStatus.SUCCESS, Path("leaderboard.md"))
        destination = Path(app.benchmarks.database.path.parent) / "leaderboard.md"
        options = LeaderboardReportOptions(include_model_details=True)
        with patch.object(app, "prompt_path", return_value=str(destination)), patch.object(
            app, "prepare_export_destination", return_value=destination
        ):
            app._model_leaderboard_report_screen(options)
        self.assertTrue(app.reporting.write_markdown_report.call_args.kwargs["include_model_details"])

    def test_session_report_selects_catalog_session_and_calls_reporting_service(self) -> None:
        app, _ = self.app_with(["3", "y", "b", "b"])
        session = app.catalog.sessions.create(BenchmarkSession(title="July session", started_at="2026-07-10"))
        aggregate = self.run_aggregate()
        aggregate.run.session_id = session.id
        aggregate = BenchmarkRunAggregate(aggregate.run, aggregate.score, session)
        report = build_session_report(session, [aggregate])
        app.reporting = Mock(spec=ReportingService)
        app.reporting.session_report.return_value = report
        app.reporting.write_markdown_report.return_value = ReportWriteResult(ReportWriteStatus.SUCCESS, Path("session.md"))
        destination = Path(app.benchmarks.database.path.parent) / "session.md"
        options = SessionReportOptions(filters=BenchmarkReportFilters(session_id=session.id))
        with patch.object(app, "prompt_path", return_value=str(destination)), patch.object(
            app, "prepare_export_destination", return_value=destination
        ):
            app._session_report_screen(options)
        app.reporting.session_report.assert_called_once_with(
            session.id,
            filters=options.filters,
            include_prompt_text=False,
            include_raw_model_output=False,
            include_attachment_metadata=False,
            title=options.title,
        )
        app.reporting.write_markdown_report.assert_called_once()

    def test_session_report_handles_no_session_without_writing(self) -> None:
        app, output = self.app_with(["3", "b", "b"])
        app.reporting = Mock(spec=ReportingService)
        app._session_report_screen(SessionReportOptions())
        self.assertIn("No Session Selected", "\n".join(output))
        app.reporting.session_report.assert_not_called()
        app.reporting.write_markdown_report.assert_not_called()

    def test_hardware_report_preview_uses_structured_group_properties(self) -> None:
        app, output = self.app_with(["2", "b", "b"])
        report = build_hardware_report([self.run_aggregate()])
        app.reporting = Mock(spec=ReportingService)
        app.reporting.hardware_report.return_value = report
        app._hardware_report_screen(HardwareReportOptions())
        rendered = "\n".join(output)
        self.assertIn("Hardware group count: 1", rendered)
        self.assertIn("Fastest group: Unknown hardware", rendered)
        self.assertIn("Highest average-score group: Unknown hardware", rendered)
        app.reporting.write_markdown_report.assert_not_called()

    def test_hardware_report_empty_selection_does_not_write(self) -> None:
        app, output = self.app_with(["3", "b", "b"])
        app.reporting = Mock(spec=ReportingService)
        app.reporting.hardware_report.return_value = build_hardware_report([])
        app._hardware_report_screen(HardwareReportOptions())
        self.assertIn("No Hardware Report Runs", "\n".join(output))
        app.reporting.write_markdown_report.assert_not_called()

    def test_hardware_report_propagates_filters_and_detail_option(self) -> None:
        app, _ = self.app_with(["3", "y", "b", "b"])
        report = build_hardware_report([self.run_aggregate()])
        app.reporting = Mock(spec=ReportingService)
        app.reporting.hardware_report.return_value = report
        app.reporting.write_markdown_report.return_value = ReportWriteResult(ReportWriteStatus.SUCCESS, Path("hardware.md"))
        destination = Path(app.benchmarks.database.path.parent) / "hardware.md"
        from engine.reporting import BenchmarkReportFilters

        options = HardwareReportOptions(
            filters=BenchmarkReportFilters(benchmark_type="code_review", hardware="RTX"),
            include_hardware_details=True,
        )
        with patch.object(app, "prompt_path", return_value=str(destination)), patch.object(
            app, "prepare_export_destination", return_value=destination
        ):
            app._hardware_report_screen(options)
        app.reporting.hardware_report.assert_called_once_with(
            filters=options.filters,
            include_prompt_text=False,
            include_raw_model_output=False,
            include_attachment_metadata=False,
            include_hardware_details=True,
            title=options.title,
        )
        self.assertTrue(app.reporting.write_markdown_report.call_args.kwargs["template_options"].include_hardware_details)

    def test_template_selection_uses_vertical_choices_and_preserves_filters(self) -> None:
        app, _ = self.app_with(["7", "1", "b"])
        from engine.reporting import BenchmarkReportFilters

        options = HardwareReportOptions(filters=BenchmarkReportFilters(benchmark_type="code_review"))
        updated = app._hardware_report_options_screen(options)
        self.assertEqual(updated.template_id, "concise")
        self.assertEqual(updated.filters, options.filters)
        self.assertFalse(updated.include_prompt_text)
        self.assertFalse(updated.include_hardware_details)


if __name__ == "__main__":
    unittest.main()
