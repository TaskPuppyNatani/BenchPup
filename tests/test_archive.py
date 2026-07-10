import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine.archive import ARCHIVE_FORMAT, ArchiveError, ArchiveService, TABLES
from engine.database import EngineDatabase
from engine.domain import (BenchmarkDefinition, BenchmarkRun, BenchmarkSession, ExportProfile, HardwareProfile,
                           ModelProfile, PromptTemplate, ReviewScore, RunAttachment, ScoreboardEntry, ScoreboardImportBatch)
from engine.services import BenchmarkService


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = EngineDatabase(Path(self.directory.name) / "source.db")
        self.database.migrate()
        self.service = BenchmarkService(self.database)
        self.archive = ArchiveService(self.database)

    def tearDown(self):
        self.directory.cleanup()

    def seed_all_entities(self):
        catalog = self.service.catalog
        session = catalog.sessions.create(BenchmarkSession(title="July"))
        model = catalog.model_profiles.create(ModelProfile(name="Qwen", model_name="Qwen 3"))
        hardware = catalog.hardware_profiles.create(HardwareProfile(name="Desktop"))
        definition = catalog.benchmark_definitions.create(BenchmarkDefinition(name="Review", file_path="main.py", benchmark_type="code_review"))
        prompt = catalog.prompt_templates.create(PromptTemplate(name="Review", version="1", prompt_text="Review this", prompt_hash=__import__("hashlib").sha256(b"Review this").hexdigest(), benchmark_type="code_review"))
        run = self.service.runs.create(BenchmarkRun(raw_model_output="Output", session_id=session.id, model_profile_id=model.id, hardware_profile_id=hardware.id, benchmark_definition_id=definition.id, prompt_template_id=prompt.id, fingerprint="archive-run"))
        self.service.scores.create(ReviewScore(run_id=run.id, overall_score=4))
        self.service.attachments.create(RunAttachment(run_id=run.id, attachment_type="log", file_path="C:/log.txt", original_filename="log.txt"))
        batch = catalog.scoreboard_import_batches.create(ScoreboardImportBatch(name="July board", source_file="board.csv"))
        catalog.scoreboard_entries.create(ScoreboardEntry(model_name="Qwen", score=4, import_batch_id=batch.id))
        catalog.export_profiles.create(ExportProfile(name="Archive", format="json"))

    def test_complete_utf8_atomic_archive_export_and_metadata(self):
        self.seed_all_entities()
        path = Path(self.directory.name) / "backup.json"
        saved = self.archive.export(path, "0.3")
        raw = saved.read_text(encoding="utf-8")
        archive = json.loads(raw)
        self.assertEqual((archive["format"], archive["archive_version"], archive["benchpup_version"]), (ARCHIVE_FORMAT, 1, "0.3"))
        self.assertEqual(set(archive["data"]), set(TABLES))
        self.assertTrue(all(archive["counts"][table] >= 1 for table in TABLES))
        self.assertEqual(list(Path(self.directory.name).glob(".backup.json.*.tmp")), [])

    def test_preview_is_read_only_and_merge_round_trip_preserves_relationships(self):
        self.seed_all_entities()
        archive = self.archive.build_archive("0.3")
        target_db = EngineDatabase(Path(self.directory.name) / "target.db"); target_db.migrate()
        target = ArchiveService(target_db)
        self.assertEqual(sum(target.preview(archive)["counts"].values()), sum(len(rows) for rows in archive["data"].values()))
        report = target.merge(archive)
        self.assertEqual(report.created["benchmark_runs"], 1)
        with target_db.connection() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_scores").fetchone()[0], 1)

    def test_merge_skips_exact_duplicates_and_rolls_back_invalid_relationship(self):
        self.seed_all_entities()
        archive = self.archive.build_archive("0.3")
        self.archive.merge(archive)
        duplicate = self.archive.merge(archive)
        self.assertEqual(duplicate.skipped["benchmark_runs"], 1)
        broken = json.loads(json.dumps(archive))
        broken["data"]["review_scores"][0]["run_id"] = 999999
        before = len(self.service.scores.list())
        with self.assertRaises(ArchiveError):
            self.archive.merge(broken)
        self.assertEqual(len(self.service.scores.list()), before)

    def test_replace_creates_safety_backup_and_reopens_database(self):
        self.seed_all_entities()
        archive = self.archive.build_archive("0.3")
        safety = self.archive.replace(archive, "0.3")
        self.assertTrue(safety.exists())
        reopened = EngineDatabase(self.database.path); reopened.migrate()
        with reopened.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()[0], 1)

    def test_rejects_malformed_wrong_format_and_future_versions(self):
        path = Path(self.directory.name) / "broken.json"; path.write_text("{", encoding="utf-8")
        with self.assertRaises(ArchiveError): self.archive.load(path)
        archive = self.archive.build_archive("0.3")
        archive["format"] = "other"
        with self.assertRaises(ArchiveError): self.archive.validate(archive)
        archive["format"], archive["archive_version"] = ARCHIVE_FORMAT, 99
        with self.assertRaises(ArchiveError): self.archive.validate(archive)
