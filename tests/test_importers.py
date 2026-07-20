import codecs
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine.database import EngineDatabase
from engine.exporters import export_combined_markdown, export_scoreboard_csv, export_scoreboard_html
from engine.importers import (
    AMBIGUOUS_IMPORT,
    BENCHMARK_RUN_IMPORT,
    UNSUPPORTED_IMPORT,
    SCOREBOARD_IMPORT,
    CsvImportService,
    ImportMappingError,
    UnsupportedCsvEncodingError,
    normalize_context_length,
    normalize_heading,
)
from engine.domain import PromptTemplate
from engine.services import BenchmarkService


class CsvImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        database = EngineDatabase(Path(self.directory.name) / "engine.db")
        database.migrate()
        self.service = BenchmarkService(database)
        self.importer = CsvImportService(self.service)

    def tearDown(self):
        self.directory.cleanup()

    def test_heading_normalization_and_utf8_bom_preview(self):
        path = Path(self.directory.name) / "sheet.csv"
        path.write_text("Model,Experts,Context,tok/s,Review Quality,Score,Hallucinations,Consistency,Reliability Score,Notes Extra,LEGEND,IT\nQwen,8,32768,120,Good,4.5,Low,High,9,Useful,key,value\n", encoding="utf-8-sig")
        preview = self.importer.preview(path)
        self.assertEqual(preview.rows[0]["model_name"], "Qwen")
        self.assertEqual(preview.rows[0]["moe_experts"], "8")
        self.assertEqual(preview.rows[0]["tokens_per_second"], "120")
        self.assertEqual(preview.rows[0]["reliability_level"], "High")
        self.assertEqual(preview.mapping["Review Quality"], "review_quality_notes")
        self.assertEqual(preview.mapping["Reliability Score"], "reliability_score")
        self.assertEqual(preview.mapping["LEGEND"], None)
        self.assertEqual(preview.unknown_headings, ["LEGEND", "IT"])
        self.assertEqual(normalize_heading(" Top P "), "top_p")
        self.assertEqual(normalize_heading("Modle"), "model_name")  # fuzzy match after aliases

    def test_detection_reports_type_encoding_and_source_rows(self):
        path = Path(self.directory.name) / "runs.csv"
        path.write_text(
            "Model,Benchmark,Prompt Text,Raw Model Output\nQwen,main.py,prompt,output\n",
            encoding="utf-8-sig",
        )
        detection = self.importer.detect(path)
        self.assertEqual((detection.status, detection.import_type, detection.encoding), (BENCHMARK_RUN_IMPORT, BENCHMARK_RUN_IMPORT, "utf-8-sig"))
        self.assertEqual(detection.preview.source_rows[0]["Model"], "Qwen")  # type: ignore[union-attr]
        self.assertEqual(detection.preview.row_numbers, [2])  # type: ignore[union-attr]

    def test_detection_distinguishes_scoreboard_and_ambiguous_shapes(self):
        scoreboard = Path(self.directory.name) / "scoreboard.csv"
        scoreboard.write_text("Model,Score,Notes\nQwen,4.5,Useful\n", encoding="utf-8")
        detection = self.importer.detect(scoreboard)
        self.assertEqual((detection.status, detection.import_type), (SCOREBOARD_IMPORT, SCOREBOARD_IMPORT))

        partial_run = Path(self.directory.name) / "partial-run.csv"
        partial_run.write_text("Model,Prompt Text,Raw Model Output\nQwen,prompt,output\n", encoding="utf-8")
        detection = self.importer.detect(partial_run)
        self.assertEqual((detection.status, detection.import_type), (AMBIGUOUS_IMPORT, None))

    def test_unrecognized_headings_are_unsupported(self):
        path = Path(self.directory.name) / "unknown-shape.csv"
        path.write_text("Alpha,Beta\nvalue,other\n", encoding="utf-8")
        detection = self.importer.detect(path)
        self.assertEqual((detection.status, detection.import_type), (UNSUPPORTED_IMPORT, None))
        self.assertIn("do not match", detection.reason)

    def test_mapping_validation_rejects_collisions_and_missing_required_fields(self):
        collision = self.importer.validate_mapping(
            {"Model A": "model_name", "Model B": "model_name"},
            BENCHMARK_RUN_IMPORT,
        )
        self.assertFalse(collision.is_valid)
        self.assertEqual(collision.errors[0].destination, "model_name")
        self.assertEqual(collision.errors[0].source_headings, ("Model A", "Model B"))

        missing = self.importer.validate_mapping({"Score": "overall_score"}, BENCHMARK_RUN_IMPORT)
        self.assertFalse(missing.is_valid)
        self.assertIn("model_name", missing.errors[0].message)

        path = Path(self.directory.name) / "collision.csv"
        path.write_text("Model A,Model B,Benchmark,Prompt Text,Raw Model Output\nQwen,Other,main.py,prompt,output\n", encoding="utf-8")
        with self.assertRaises(ImportMappingError):
            self.importer.preview(path, {"Model A": "model_name", "Model B": "model_name"})

    def test_preview_preserves_physical_source_line_numbers(self):
        path = Path(self.directory.name) / "physical-lines.csv"
        path.write_text(
            "Model,Benchmark,Prompt Text,Raw Model Output\n"
            "Qwen,main.py,\"first line\nsecond line\",output\n"
            "\n"
            "Llama,main.py,prompt,output\n",
            encoding="utf-8",
        )
        preview = self.importer.preview(path)
        self.assertEqual(preview.row_numbers, [3, 5])
        self.assertEqual(preview.source_row_numbers, [3, 5])

    def test_utf32_is_rejected_before_utf16_detection(self):
        for name, bom, encoding in (
            ("utf32-le.csv", codecs.BOM_UTF32_LE, "utf-32-le"),
            ("utf32-be.csv", codecs.BOM_UTF32_BE, "utf-32-be"),
        ):
            path = Path(self.directory.name) / name
            path.write_bytes(bom + "Model,Score\nQwen,4.5\n".encode(encoding))
            detection = self.importer.detect(path)
            self.assertEqual((detection.status, detection.encoding), (UNSUPPORTED_IMPORT, encoding))
            with self.assertRaisesRegex(UnsupportedCsvEncodingError, encoding):
                self.importer.detect_encoding(path)

    def test_validation_is_non_mutating_and_preserves_original_row_numbers(self):
        rows = [
            {"model_name": "Good", "raw_model_output": "valid"},
            {"model_name": "Bad", "overall_score": "6", "raw_model_output": "invalid"},
        ]
        validation = self.importer.validate_rows(
            rows,
            import_type=BENCHMARK_RUN_IMPORT,
            row_numbers=[8, 11],
        )
        self.assertFalse(validation.is_valid)
        self.assertEqual(validation.errors[0].row_number, 11)
        self.assertEqual(self.service.runs.list(), [])

    def test_commit_rollback_preserves_original_row_number(self):
        rows = [
            {"model_name": "Good", "raw_model_output": "valid"},
            {"model_name": "Bad", "overall_score": "6", "raw_model_output": "invalid"},
        ]
        with self.assertRaisesRegex(ValueError, r"Row 11: overall_score must be between 0 and 5"):
            self.importer.import_rows(rows, row_numbers=[8, 11])
        self.assertEqual(self.service.runs.list(), [])

    def test_scoreboard_validation_uses_original_rows_without_creating_batch(self):
        validation = self.importer.validate_rows(
            [{"model_name": "Bad", "context_length": "not-a-context"}],
            import_type=SCOREBOARD_IMPORT,
            row_numbers=[17],
            source_file="scoreboard.csv",
        )
        self.assertFalse(validation.is_valid)
        self.assertEqual(validation.errors[0].row_number, 17)
        self.assertEqual(self.service.catalog.scoreboard_entries.list(), [])
        self.assertEqual(self.service.catalog.scoreboard_import_batches.list(), [])

    def test_unknown_columns_are_ignored_by_default(self):
        path = Path(self.directory.name) / "unknown.csv"
        path.write_text("Model Name,Spreadsheet Legend\nQwen,ignore me\n", encoding="utf-8")
        preview = self.importer.preview(path)
        self.assertIsNone(preview.mapping["Spreadsheet Legend"])
        self.assertNotIn("Spreadsheet Legend", preview.rows[0])

    def test_mapping_profiles_are_saved_and_loaded(self):
        mapping = {"Model": "model_name", "Ignore": None}
        self.importer.save_mapping_profile("My Spreadsheet", mapping)
        _, name, loaded = self.importer.mapping_profiles()[0]
        self.assertEqual(name, "My Spreadsheet")
        self.assertEqual(loaded, mapping)

    def test_scoreboard_csv_maps_and_imports_historical_entries(self):
        path = Path(self.directory.name) / "scoreboard.csv"
        path.write_text("Model Name,Temp,Experts,Context,tok/s,Review Quality,Score,Hallucinations,Consistency,Reliability Score,Verdict,Notes,Notes Extra,LEGEND\nQwen,0.3,8,32768,120,Strong,4.5,Low,High,9,Useful,Primary,Imported,ignore\n", encoding="utf-8")
        preview = self.importer.preview(path, summary=True)
        self.assertEqual(preview.mapping["Review Quality"], "review_quality")
        self.assertEqual(preview.mapping["Score"], "score")
        self.assertEqual(preview.mapping["LEGEND"], None)
        self.importer.import_scoreboard_entries(preview.rows, path, "Historical July")
        entry = self.service.catalog.scoreboard_entries.list()[0]
        batch = self.service.catalog.scoreboard_import_batches.list()[0]
        self.assertEqual((entry.model_name, entry.score, entry.consistency), ("Qwen", 4.5, "High"))
        self.assertEqual(entry.notes_extra, "Imported")
        self.assertEqual((entry.import_batch_id, batch.name), (batch.id, "Historical July"))
        csv_path = export_scoreboard_csv(self.service.catalog, Path(self.directory.name) / "scoreboard-export.csv")
        report_path = export_combined_markdown(self.service, self.service.catalog, Path(self.directory.name) / "report.md")
        self.assertIn("Historical July", csv_path.read_text(encoding="utf-8"))
        self.assertIn("### Historical July", report_path.read_text(encoding="utf-8"))
        html_path = export_scoreboard_html(self.service.catalog, Path(self.directory.name) / "scoreboard-report.html")
        html = html_path.read_text(encoding="utf-8")
        self.assertIn("<!doctype html>", html)
        self.assertIn("Historical July", html)
        self.assertIn("Generated at", html)
        self.assertIn('id="search"', html)
        self.assertIn('id="hallucination"', html)
        self.assertIn('data-sort="score"', html)
        self.assertIn('class="data-row"', html)
        self.assertIn("Total entries", html)

    def test_html_report_includes_escaped_prompt_template_viewer(self):
        text = "# Prompt\n\n<script>alert('no')</script>\n\n    code fence indentation\n"
        self.service.catalog.prompt_templates.create(PromptTemplate(
            name="<unsafe>", version="1.0", prompt_text=text,
            prompt_hash=__import__("hashlib").sha256(text.encode()).hexdigest(), benchmark_type="code_review",
            notes="<notes>", is_active=False,
        ))
        html_path = export_scoreboard_html(self.service.catalog, Path(self.directory.name) / "report.html")
        html = html_path.read_text(encoding="utf-8")
        self.assertIn('id="prompt-templates-section"', html)
        self.assertIn("Total prompt templates", html)
        self.assertIn('id="template-search"', html)
        self.assertIn('id="template-type"', html)
        self.assertIn('id="template-active"', html)
        self.assertIn('class="prompt-text"', html)
        self.assertIn("&lt;script&gt;alert(&#x27;no&#x27;)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert('no')</script>", html)
        self.assertIn("    code fence indentation", html)
        self.assertIn("data-template-search", html)
        self.assertIn("Inactive", html)

    def test_scoreboard_context_values_are_normalized_in_preview_and_import(self):
        path = Path(self.directory.name) / "scoreboard-context.csv"
        path.write_text(
            "Model,Context\n"
            "K lower,32k\nK upper,32K\nMillion,1M\nComma,\"8,192\"\nBlank,\nDash,-\nNot applicable,N/A\n",
            encoding="utf-8",
        )
        preview = self.importer.preview(path, summary=True)
        self.assertEqual(
            [row["context_length"] for row in preview.rows],
            ["32000", "32000", "1000000", "8192", "", "", ""],
        )
        self.importer.import_scoreboard_entries(preview.rows, path)
        self.assertEqual(
            [entry.context_length for entry in self.service.catalog.scoreboard_entries.list()],
            [32000, 32000, 1000000, 8192, None, None, None],
        )

    def test_scoreboard_invalid_context_reports_original_value(self):
        path = Path(self.directory.name) / "invalid-context.csv"
        path.write_text("Model,Context\nGood,128k\nBad,a lot\n", encoding="utf-8")
        preview = self.importer.preview(path, summary=True)
        with self.assertRaisesRegex(ValueError, r"Row 3: context_length must be an integer \(got 'a lot'\)"):
            self.importer.import_scoreboard_entries(preview.rows, path)
        self.assertEqual(self.service.catalog.scoreboard_entries.list(), [])

    def test_scoreboard_preview_skips_non_data_rows_and_imports_the_preview(self):
        path = Path(self.directory.name) / "scoreboard-with-footer.csv"
        path.write_text(
            "Model,Score,Notes\n"
            "Qwen,4.5,Useful\n"
            ",,\n"
            "LEGEND,,Scores are out of five\n"
            "Notes,,Imported from July sheet\n"
            ",,,\n"
            "Llama,4.0,Good\n",
            encoding="utf-8",
        )
        preview = self.importer.preview(path, summary=True)
        self.assertEqual([row["model_name"] for row in preview.rows], ["Qwen", "Llama"])
        self.assertEqual(preview.skipped_rows, [(3, "blank row"), (4, "metadata row"), (5, "metadata row"), (6, "blank row")])
        result = self.importer.import_scoreboard_entries(preview.rows, path, row_numbers=preview.row_numbers)
        self.assertEqual(result.imported, len(preview.rows))
        self.assertEqual(len(self.service.catalog.scoreboard_entries.list()), len(preview.rows))

    def test_scoreboard_row_with_score_but_no_model_is_an_error(self):
        path = Path(self.directory.name) / "missing-model.csv"
        path.write_text("Model,Score,Notes\n,4.5,Useful\n", encoding="utf-8")
        preview = self.importer.preview(path, summary=True)
        self.assertEqual(len(preview.rows), 1)
        with self.assertRaisesRegex(ValueError, "Row 2: model_name is required"):
            self.importer.import_scoreboard_entries(preview.rows, path, row_numbers=preview.row_numbers)

    def test_context_normalization_supports_decimal_suffixes_and_commas(self):
        self.assertEqual(normalize_context_length("128k"), 128000)
        self.assertEqual(normalize_context_length("1m"), 1000000)
        self.assertEqual(normalize_context_length("8192"), 8192)

    def test_duplicate_policies_skip_replace_and_keep(self):
        row = {"model_name": "Qwen", "benchmark_file": "main.py", "raw_model_output": "Review"}
        self.assertEqual(self.importer.import_rows([row]).imported, 1)
        skipped = self.importer.import_rows([row], "skip")
        self.assertEqual((skipped.imported, skipped.skipped, skipped.duplicates), (0, 1, 1))
        replaced = self.importer.import_rows([row], "replace")
        self.assertEqual((replaced.imported, replaced.replaced), (1, 1))
        kept = self.importer.import_rows([row], "keep")
        self.assertEqual((kept.imported, kept.duplicates), (1, 1))
        self.assertEqual(len(self.service.runs.list()), 2)

    def test_validation_failure_rolls_back_entire_batch(self):
        rows = [
            {"model_name": "Good", "raw_model_output": "valid"},
            {"model_name": "Bad", "overall_score": "6", "raw_model_output": "invalid"},
        ]
        with self.assertRaisesRegex(ValueError, "Row 3.*overall_score"):
            self.importer.import_rows(rows)
        self.assertEqual(self.service.runs.list(), [])

    def test_successful_import_writes_run_and_review(self):
        row = {"model_name": "Qwen", "backend": "LM Studio", "benchmark_file": "main.py",
               "benchmark_type": "code_review", "raw_model_output": "Found a bug", "overall_score": "4.5",
               "hallucination_level": "Low", "reliability_level": "High", "verdict": "Useful"}
        result = self.importer.import_rows([row])
        run = self.service.runs.list()[0]
        _, score, _ = self.service.get_run(run.id)
        self.assertEqual(result.imported, 1)
        self.assertEqual(run.model_snapshot["backend"], "LM Studio")
        self.assertEqual(run.benchmark_snapshot["benchmark_file"], "main.py")
        self.assertEqual(score.overall_score, 4.5)
        self.assertEqual(score.verdict, "Useful")
