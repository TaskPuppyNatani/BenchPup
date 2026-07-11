import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cli import APP_VERSION, BACK, CANCEL, QuitApplication, TerminalApp
from engine.domain import BenchmarkDefinition, ModelProfile, PromptTemplate, ScoreboardEntry, ScoreboardImportBatch


def score_answers():
    return ["4", "", "", "", "", "", "", "4", "", "", "Good", ""]


class CliPolishTests(unittest.TestCase):
    def app_with(self, answers):
        directory = tempfile.TemporaryDirectory()
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        self.addCleanup(directory.cleanup)
        return app, output

    def seed_catalog(self, app):
        app.catalog.model_profiles.create(ModelProfile(name="Local Qwen", model_name="Qwen 3"))
        app.catalog.benchmark_definitions.create(BenchmarkDefinition(name="Speech review", file_path="speech_server.py", benchmark_type="code_review"))
        text = "Review the code"
        app.catalog.prompt_templates.create(PromptTemplate(name="Review prompt", version="1.0", prompt_text=text, prompt_hash=hashlib.sha256(text.encode()).hexdigest(), benchmark_type="code_review"))

    def test_wizard_reviews_and_edits_before_save(self):
        answers = iter(["", "1", "1", "1", "", "draft output", *score_answers(), "n", "e", "6", "final output", *score_answers(), "n", "s"])
        app, _ = self.app_with(answers); self.seed_catalog(app)
        app.add_run_wizard()
        runs = app.benchmarks.runs.list()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].raw_model_output, "final output")

    def test_create_session_from_cli_does_not_pass_duplicate_timestamps(self):
        app, _ = self.app_with(iter(["July import", "Historical entries", "", "", "Imported from CSV"]))
        session = app.create_session()
        self.assertEqual(session.title, "July import")
        self.assertIsNone(session.started_at)
        self.assertIsNone(session.completed_at)

    def test_invalid_integer_and_float_are_reprompted(self):
        app, output = self.app_with(iter(["abc", "3", "bad", "2.5"]))
        self.assertEqual(app.ask_id(), 3)
        self.assertEqual(app.ask_float("Temperature"), 2.5)
        self.assertTrue(any("not a valid Run ID" in line for line in output))
        self.assertTrue(any("not a valid number" in line for line in output))

    def test_back_and_cancel_commands_are_case_insensitive(self):
        app, _ = self.app_with(iter(["BACK", "Cancel"]))
        self.assertIs(app.ask("Field", navigation=True), BACK)
        self.assertIs(app.ask("Field", navigation=True), CANCEL)

    def test_uppercase_menu_command_and_quit_are_accepted(self):
        app, output = self.app_with(iter(["LIST", "", "Quit"]))
        app.run()
        self.assertIn("No benchmark runs found.", output)

    def test_quit_all_normalizes_case_and_spaces(self):
        app, _ = self.app_with(iter(["  QUIT   ALL  "]))
        self.assertEqual(app.normalized("  QUIT   ALL  "), "quit all")
        app.run()

    def test_quit_all_exits_from_main_menu(self):
        app, output = self.app_with(iter(["qa"]))
        app.run()
        self.assertIn("Exiting BenchPup.", output)

    def test_quit_all_exits_from_submenu_and_wizard(self):
        app, output = self.app_with(iter(["11", "QA"]))
        app.run()
        self.assertIn("Exiting BenchPup.", output)
        app, output = self.app_with(iter(["1", "Quit All"]))
        app.run()
        self.assertIn("Exiting BenchPup.", output)

    def test_quit_all_exits_from_path_prompt(self):
        app, output = self.app_with(iter(["15", "Quit A"]))
        app.run()
        self.assertIn("Exiting BenchPup.", output)

    def test_help_describes_q_and_global_quit_all_without_escape(self):
        app, output = self.app_with(iter([""]))
        app.help()
        rendered = "\n".join(output)
        self.assertIn("Q   Back / Cancel current screen", rendered)
        self.assertIn("QA  Quit BenchPup completely", rendered)
        self.assertIn("qa, quit all, quit a", rendered)
        self.assertNotIn("Escape", rendered)

    def test_main_menu_uses_grouped_vertical_layout(self):
        app, output = self.app_with(iter(["q"]))
        app.run()
        menu = "\n".join(output)
        self.assertIn("Runs\n----\n1) Add Run", menu)
        self.assertIn("Reference Data\n--------------\n6) Sessions", menu)
        self.assertIn("Data\n----\n11) Import", menu)
        self.assertIn("13) Scoreboard", menu)
        self.assertIn("Scoreboard entries : 0", menu)
        self.assertIn("Q) Quit", menu)
        self.assertIn("QA) Quit BenchPup completely", menu)
        self.assertIn("Help\n----\nH) Help", menu)
        self.assertIn(f"Version {APP_VERSION}", menu)
        self.assertIn("Database : benchmarks.db", menu)

    def test_title_lines_are_centered_to_the_menu_width(self):
        app, output = self.app_with(iter(["q"]))
        app.run()
        title = next(line for line in output if "BenchPup" in line)
        version = next(line for line in output if f"Version {APP_VERSION}" in line)
        self.assertEqual(len(title), len(version))
        title_center = title.index("BenchPup") + len("BenchPup") / 2
        version_center = version.index("Version") + len(f"Version {APP_VERSION}") / 2
        self.assertLessEqual(abs(title_center - version_center), 0.5)

    def test_shared_renderer_frames_header_after_interactive_clear(self):
        app, output = self.app_with(iter([]))
        app.interactive_input = True
        with patch("cli.sys.stdout.isatty", return_value=True), patch("cli.os.system") as clear:
            app.render_screen("Settings", "Body")
        divider = "=" * 56
        self.assertEqual(output[:4], [divider, "BenchPup".center(56), f"Version {APP_VERSION}".center(56), divider])
        self.assertEqual(output[-1], divider)
        clear.assert_called_once()

    def test_keyboard_interrupt_at_main_menu_exits_without_reprompting(self):
        calls = iter([KeyboardInterrupt()])
        def interrupted_input(_):
            value = next(calls)
            if isinstance(value, BaseException): raise value
            return value
        directory = tempfile.TemporaryDirectory(); output = []
        self.addCleanup(directory.cleanup)
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=interrupted_input, output_fn=output.append)
        app.run()
        self.assertTrue(any("Operation cancelled." in line for line in output))
        self.assertIn("Exiting BenchPup.", output)

    def test_keyboard_interrupt_in_submenu_cancels_operation(self):
        def interrupted_input(_):
            raise KeyboardInterrupt()
        directory = tempfile.TemporaryDirectory(); output = []
        self.addCleanup(directory.cleanup)
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=interrupted_input, output_fn=output.append)
        app.import_screen()
        self.assertTrue(any("Operation cancelled." in line for line in output))

    def test_import_menu_flow_imports_csv(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "runs.csv"
        source.write_text("Model Name,Benchmark,Prompt Text,Raw Model Output,Overall Score\nQwen,main.py,Review it,Found a bug,4\n", encoding="utf-8")
        answers = iter([str(source), "y", "y", "s"])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.import_csv("runs")
        self.assertEqual(len(app.benchmarks.runs.list()), 1)
        self.assertTrue(any("Imported 1" in line for line in output))

    def test_prompt_file_import_is_available_from_import_and_prompt_template_screens(self):
        app, _ = self.app_with(iter(["5"]))
        import_action = Mock()
        app.import_prompt_template_file = import_action
        app.import_screen()
        import_action.assert_called_once_with()

        app, _ = self.app_with(iter(["4", "b"]))
        import_action = Mock()
        app.import_prompt_template_file = import_action
        app.prompt_templates_screen()
        import_action.assert_called_once_with()

    def test_prompt_templates_screen_uses_a_vertical_menu_without_old_command_bar(self):
        app, output = self.app_with(iter(["b"]))
        app.prompt_templates_screen()
        rendered = "\n".join(output)
        self.assertIn("1) List Templates\n2) View Template\n3) New Template", rendered)
        self.assertIn("4) Import Template From File\n5) Edit Template\n6) Export Template\n7) Delete Template", rendered)
        self.assertIn("B) Back\nQA) Quit BenchPup completely", rendered)
        self.assertNotIn("Q) Back / Quit", rendered)
        self.assertNotIn("N) New  I) Import raw prompt file", rendered)

    def test_prompt_templates_numbered_actions_reuse_existing_handlers(self):
        app, output = self.app_with(iter(["1", "", "2", "1", "3", "4", "5", "1", "6", "1", "7", "1", "b"]))
        text = "Prompt"
        template = app.catalog.prompt_templates.create(PromptTemplate(name="One", version="1", prompt_text=text, prompt_hash=hashlib.sha256(text.encode()).hexdigest(), benchmark_type="code_review"))
        assert template.id is not None
        app.create_prompt_template = Mock(return_value=None)
        app.import_prompt_template_file = Mock()
        app.view_prompt_template = Mock()
        app.edit_prompt_template = Mock()
        app.export_prompt_template = Mock()
        app.delete_prompt_template = Mock()
        app.prompt_templates_screen()
        rendered = "\n".join(output)
        self.assertIn("1) One v1 (code_review)", rendered)
        app.create_prompt_template.assert_called_once_with()
        app.import_prompt_template_file.assert_called_once_with()
        for handler in (app.view_prompt_template, app.edit_prompt_template, app.export_prompt_template, app.delete_prompt_template):
            handler.assert_called_once_with(template.id)

    def test_prompt_template_submenus_return_locally_and_quit_all_propagates(self):
        app, _ = self.app_with(iter(["q"]))
        self.assertIs(app.select_prompt_template(), BACK)
        app, _ = self.app_with(iter(["b"]))
        app.prompt_templates_screen()
        app, _ = self.app_with(iter(["QA"]))
        with self.assertRaises(QuitApplication):
            app.prompt_templates_screen()

    def test_import_prompt_file_preserves_raw_markdown_and_uses_filename_stem(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "review template.md"
        text = "# Review\n\n- Preserve this\n\n```python\n  return value\n```\n"
        source.write_bytes(text.encode("utf-8"))
        answers = iter([str(source), "", "", "", "", "y", "y"])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.import_prompt_template_file()
        templates = app.catalog.prompt_templates.list()
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0].name, "review template")
        self.assertEqual(templates[0].version, "1.0")
        self.assertEqual(templates[0].prompt_text, text)
        self.assertEqual(templates[0].prompt_hash, hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertTrue(any("Prompt template imported successfully" in line for line in output))

    def test_import_prompt_file_allows_unfamiliar_readable_extension(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "raw-prompt.custom"
        source.write_text("Use the complete file.", encoding="utf-8")
        answers = iter([str(source), "", "", "", "", "y", "y"])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.import_prompt_template_file()
        self.assertEqual(app.catalog.prompt_templates.list()[0].prompt_text, "Use the complete file.")
        self.assertTrue(any("unfamiliar" in line for line in output))

    def test_import_prompt_file_duplicate_name_and_version_can_choose_new_version(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "review.txt"
        source.write_text("New prompt", encoding="utf-8")
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: "", output_fn=lambda _: None)
        old_text = "Existing prompt"
        app.catalog.prompt_templates.create(PromptTemplate(name="review", version="1.0", prompt_text=old_text, prompt_hash=hashlib.sha256(old_text.encode()).hexdigest(), benchmark_type="code_review"))
        answers = iter([str(source), "", "", "v", "2.0", "", "", "y", "y"])
        app.input = lambda _: next(answers)
        app.interactive_input = False
        app.import_prompt_template_file()
        templates = app.catalog.prompt_templates.list()
        self.assertEqual([(item.name, item.version) for item in templates], [("review", "1.0"), ("review", "2.0")])

    def test_prompt_file_import_honors_local_and_global_quit_commands(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "review.txt"
        source.write_text("Prompt", encoding="utf-8")
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: "q", output_fn=lambda _: None)
        app.import_prompt_template_file()
        self.assertEqual(app.catalog.prompt_templates.list(), [])
        app = TerminalApp(Path(directory.name) / "other.db", input_fn=lambda _: "Quit A", output_fn=lambda _: None)
        with self.assertRaises(QuitApplication):
            app.import_prompt_template_file()

    def test_prompt_template_view_displays_complete_text_and_metadata(self):
        app, output = self.app_with(iter([""]))
        text = "# Heading\n\n  indented\n\n```python\nprint('complete')\n```\n"
        template = app.catalog.prompt_templates.create(PromptTemplate(
            name="Complete", version="2.0", prompt_text=text,
            prompt_hash=hashlib.sha256(text.encode()).hexdigest(), benchmark_type="code_review", notes="Keep formatting",
        ))
        assert template.id is not None
        app.view_prompt_template(template.id)
        rendered = "\n".join(output)
        self.assertIn("Name: Complete", rendered)
        self.assertIn("Version: 2.0", rendered)
        self.assertIn("SHA-256:", rendered)
        self.assertIn(text, rendered)

    def test_prompt_template_edit_recalculates_hash_after_file_replacement(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        replacement = Path(directory.name) / "replacement.md"
        replacement_text = "# New\n\n  exact indentation\n"
        replacement.write_bytes(replacement_text.encode("utf-8"))
        answers = iter(["", "", "", "", "", "y", str(replacement), "y"])
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=lambda _: None)
        original = "Original"
        template = app.catalog.prompt_templates.create(PromptTemplate(
            name="Replace", version="1.0", prompt_text=original,
            prompt_hash=hashlib.sha256(original.encode()).hexdigest(), benchmark_type="code_review",
        ))
        assert template.id is not None
        app.edit_prompt_template(template.id)
        saved = app.catalog.prompt_templates.get(template.id)
        assert saved is not None
        self.assertEqual(saved.prompt_text, replacement_text)
        self.assertEqual(saved.prompt_hash, hashlib.sha256(replacement_text.encode()).hexdigest())

    def test_prompt_template_export_writes_exact_prompt_body(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        destination = Path(directory.name) / "export.md"
        text = "# Exact\n\n    indented\n"
        answers = iter([str(destination)])
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=lambda _: None)
        template = app.catalog.prompt_templates.create(PromptTemplate(
            name="Exact", version="1", prompt_text=text,
            prompt_hash=hashlib.sha256(text.encode()).hexdigest(), benchmark_type="code_review",
        ))
        assert template.id is not None
        app.export_prompt_template(template.id)
        self.assertEqual(destination.read_bytes(), text.encode("utf-8"))

    def test_prompt_template_file_operations_use_shared_prompt_toolkit_cleanup(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        replacement = Path(directory.name) / "replacement.txt"
        replacement.write_text("Replacement", encoding="utf-8")
        destination = Path(directory.name) / "export.txt"
        answers = iter(["", "", "", "", "", "y", "y"])
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=lambda _: None)
        app.interactive_input = True
        original = "Original"
        template = app.catalog.prompt_templates.create(PromptTemplate(name="Shared", version="1", prompt_text=original, prompt_hash=hashlib.sha256(original.encode()).hexdigest(), benchmark_type="code_review"))
        assert template.id is not None
        from unittest.mock import patch
        with patch("cli.toolkit_prompt", side_effect=[str(replacement), str(destination)]), patch("cli.sys.stdout.flush") as flush:
            app.edit_prompt_template(template.id)
            app.export_prompt_template(template.id)
        self.assertGreaterEqual(flush.call_count, 2)
        self.assertEqual(destination.read_bytes(), b"Replacement")

    def test_prompt_template_view_honors_local_and_global_quit(self):
        app, _ = self.app_with(iter(["q"]))
        text = "Prompt"
        template = app.catalog.prompt_templates.create(PromptTemplate(name="Quit", version="1", prompt_text=text, prompt_hash=hashlib.sha256(text.encode()).hexdigest(), benchmark_type="code_review"))
        assert template.id is not None
        app.view_prompt_template(template.id)
        app, _ = self.app_with(iter(["QA"]))
        template = app.catalog.prompt_templates.create(PromptTemplate(name="Quit", version="1", prompt_text=text, prompt_hash=hashlib.sha256(text.encode()).hexdigest(), benchmark_type="code_review"))
        assert template.id is not None
        with self.assertRaises(QuitApplication):
            app.view_prompt_template(template.id)

    def test_import_mapping_edit_uses_numbered_choices(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "runs.csv"
        source.write_text("Model Name,Experts,Benchmark\nQwen,8,main.py\n", encoding="utf-8")
        answers = iter([str(source), "e", "2", "10", "d", "n", "y", "y", "s"])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.import_csv("runs")
        self.assertEqual(app.benchmarks.runs.list()[0].model_snapshot["moe_experts"], "8")
        self.assertTrue(any("10) moe_experts" in line for line in output))

    def test_yes_is_not_accepted_as_a_mapping_field(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "runs.csv"
        source.write_text("Model Name,Experts,Benchmark\nQwen,8,main.py\n", encoding="utf-8")
        answers = iter([str(source), "e", "2", "yes", "10", "d", "n", "y", "y", "s"])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.import_csv("runs")
        self.assertTrue(any("10) moe_experts" in line for line in output))
        self.assertEqual(len(app.benchmarks.runs.list()), 1)

    def test_import_cancel_does_not_write_rows(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "runs.csv"
        source.write_text("Model Name\nQwen\n", encoding="utf-8")
        answers = iter([str(source), "c"])
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=lambda _: None)
        app.import_csv()
        self.assertEqual(app.benchmarks.runs.list(), [])

    def test_import_default_benchmark_is_applied_to_blank_rows(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "runs.csv"
        source.write_text("Model Name,Raw Model Output\nQwen,Found a bug\n", encoding="utf-8")
        answers = iter([str(source), "y", "y", ""])
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=lambda _: None)
        app.import_csv()
        self.assertEqual(app.catalog.scoreboard_entries.list()[0].model_name, "Qwen")

    def test_import_without_benchmark_column_can_be_summary_rows(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "summary.csv"
        source.write_text("Model Name,Score\nQwen,4\n", encoding="utf-8")
        answers = iter([str(source), "y", "y", ""])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.import_csv()
        self.assertEqual(len(app.catalog.scoreboard_entries.list()), 1)
        self.assertTrue(any("Imported 1 scoreboard entry" in line for line in output))

    def test_scoreboard_preview_reports_skipped_non_data_rows(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "summary.csv"
        source.write_text("Model Name,Score\nQwen,4\nLEGEND,Out of five\n", encoding="utf-8")
        answers = iter([str(source), "y", "y", ""])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.import_csv()
        self.assertTrue(any("Preview: 1 importable row(s), 1 skipped non-data row(s)" in line for line in output))

    def test_scoreboard_browser_lists_views_and_filters_historical_entries(self):
        app, output = self.app_with(iter(["1", "2", "1", "3", "4", "1", "b"]))
        batch = app.catalog.scoreboard_import_batches.create(
            ScoreboardImportBatch(name="Historical July", source_file="july.csv", imported_at="2026-07-10T12:00:00Z")
        )
        app.catalog.scoreboard_entries.create(
            ScoreboardEntry(model_name="Qwen", score=4.5, verdict="Useful", import_batch_id=batch.id, imported_at="2026-07-10T12:00:00Z")
        )
        app.scoreboard_screen()
        rendered = "\n".join(output)
        self.assertIn("Scoreboard\n----------\nHistorical summary imports", rendered)
        self.assertIn("#1 | Qwen | score=4.5 | batch=Historical July | imported=2026-07-10T12:00:00Z", rendered)
        self.assertIn("Scoreboard entry #1", rendered)
        self.assertIn("Batch: Historical July", rendered)
        self.assertIn("#1 | Historical July | entries=1 | imported=2026-07-10T12:00:00Z | source=july.csv", rendered)

    def test_scoreboard_html_export_prompts_to_open_report(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        destination = Path(directory.name) / "scoreboard.html"
        answers = iter(["5", str(destination), "n"])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.catalog.scoreboard_entries.create(ScoreboardEntry(model_name="Qwen", notes="<script>alert(1)</script>"))
        app.export_screen()
        self.assertTrue(destination.exists())
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", destination.read_text(encoding="utf-8"))
        self.assertTrue(any("Exported Scoreboard HTML" in line for line in output))

    def test_backup_and_restore_preview_do_not_write_database(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        archive_path = Path(directory.name) / "backup.json"
        answers = iter([str(archive_path)])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.backup_data()
        self.assertTrue(archive_path.exists())
        answers = iter([str(archive_path), "1"])
        app.input = lambda _: next(answers)
        app.restore_data()
        self.assertEqual(app.benchmarks.runs.list(), [])
        self.assertTrue(any("Restore Archive" in line for line in output))
        self.assertTrue(any("Backup completed successfully" in line for line in output))

    def test_backup_default_creates_project_backups_folder(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        answers = iter([""])
        app = TerminalApp(Path(directory.name) / "data" / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=lambda _: None)
        self.addCleanup(lambda: [handler.close() for handler in app.logger.handlers])
        app.backup_data()
        backups = Path(directory.name) / "backups"
        self.assertEqual(len(list(backups.glob("benchpup-backup-*.json"))), 1)

    def test_export_creates_missing_parent_after_confirmation_and_shows_final_path(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        destination = Path(directory.name) / "new exports" / "scoreboard"
        answers = iter(["5", str(destination), "y", "n"])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.export_screen()
        expected = destination.with_suffix(".html")
        self.assertTrue(expected.exists())
        self.assertTrue(any(f"Writing export to {expected}" in line for line in output))
