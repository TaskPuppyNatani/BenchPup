import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine.database import EngineDatabase
from engine.exporters import export_combined_markdown, export_scoreboard_csv
from engine.importers import CsvImportService, normalize_heading
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
