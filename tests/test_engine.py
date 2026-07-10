import hashlib
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine.database import EngineDatabase
from engine.domain import BenchmarkDefinition, BenchmarkRun, BenchmarkSession, HardwareProfile, ModelProfile, PromptTemplate, ReviewScore, RunAttachment
from engine.services import BenchmarkService, CatalogService


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = EngineDatabase(Path(self.temporary_directory.name) / "engine.db")
        self.database.migrate()
        self.catalog = CatalogService(self.database)
        self.service = BenchmarkService(self.database, self.catalog)

    def tearDown(self): self.temporary_directory.cleanup()

    def test_schema_version_and_tables_are_created(self):
        self.database.migrate()  # migrations are safe to run repeatedly
        with self.database.connection() as connection:
            version = connection.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()["version"]
            tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(version, 4)
        self.assertTrue({"benchmark_sessions", "prompt_templates", "hardware_profiles", "run_attachments"} <= tables)

    def test_legacy_mvp_runs_are_migrated(self):
        self.temporary_directory.cleanup()
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name) / "legacy.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE benchmark_runs (id INTEGER PRIMARY KEY, raw_model_output TEXT, prompt_name TEXT, prompt_text TEXT, created_at TEXT)")
            connection.execute("INSERT INTO benchmark_runs VALUES (1, 'legacy output', 'old prompt', 'prompt text', '2026-01-01T00:00:00+00:00')")
            connection.commit()
        finally:
            connection.close()
        migrated = EngineDatabase(path); migrated.migrate()
        run = BenchmarkService(migrated).runs.get(1)
        self.assertEqual(run.raw_model_output, "legacy output")
        self.assertEqual(run.prompt_snapshot["name"], "old prompt")

    def test_catalog_and_run_crud_preserve_snapshots(self):
        session = self.catalog.sessions.create(BenchmarkSession(title="July batch"))
        model = self.catalog.model_profiles.create(ModelProfile(name="Qwen local", model_name="Qwen 3"))
        definition = self.catalog.benchmark_definitions.create(BenchmarkDefinition(name="Speech review", file_path="speech_server.py", benchmark_type="code_review"))
        text = "Review this server for bugs."
        template = self.catalog.prompt_templates.create(PromptTemplate(name="review", version="1.0", prompt_text=text, prompt_hash=hashlib.sha256(text.encode()).hexdigest(), benchmark_type="code_review"))
        hardware = self.catalog.hardware_profiles.create(HardwareProfile(name="Workstation", gpu="RTX 4090", vram_gb=24, backend_versions={"LM Studio": "0.3"}))
        run, review = self.service.save_run(BenchmarkRun(raw_model_output="Found a race condition.", session_id=session.id, model_profile_id=model.id, benchmark_definition_id=definition.id, prompt_template_id=template.id, hardware_profile_id=hardware.id), ReviewScore(run_id=0, accuracy_score=4.5, overall_score=4.5, verdict="Useful"))
        attachment = self.service.add_attachment(RunAttachment(run_id=run.id, attachment_type="log", file_path="C:/logs/run.log", original_filename="run.log"))
        read_run, read_review, attachments = self.service.get_run(run.id)
        self.assertEqual(review.run_id, run.id)
        self.assertEqual(read_run.model_snapshot["model_name"], "Qwen 3")
        self.assertEqual(read_run.prompt_snapshot["version"], "1.0")
        self.assertEqual(read_run.prompt_snapshot["prompt_hash"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(read_run.hardware_snapshot["gpu"], "RTX 4090")
        self.assertEqual(read_review.overall_score, 4.5)
        self.assertEqual(attachments[0].id, attachment.id)

    def test_validation_rejects_invalid_scores(self):
        with self.assertRaises(ValueError): ReviewScore(run_id=1, overall_score=6).validate()

    def test_catalog_records_support_update_and_delete(self):
        session = self.catalog.sessions.create(BenchmarkSession(title="Original"))
        self.assertEqual(self.catalog.sessions.update(replace(session, title="Renamed")).title, "Renamed")
        self.catalog.sessions.delete(session.id, soft=True)
        self.assertEqual(self.catalog.sessions.list(), [])

        model = self.catalog.model_profiles.create(ModelProfile(name="Model", model_name="Model A"))
        definition = self.catalog.benchmark_definitions.create(BenchmarkDefinition(name="Definition", file_path="main.py", benchmark_type="code_review"))
        prompt_text = "Review"
        prompt = self.catalog.prompt_templates.create(PromptTemplate(name="Prompt", version="1", prompt_text=prompt_text, prompt_hash=hashlib.sha256(prompt_text.encode()).hexdigest(), benchmark_type="code_review"))
        hardware = self.catalog.hardware_profiles.create(HardwareProfile(name="Hardware"))
        self.assertEqual(self.catalog.model_profiles.update(replace(model, model_name="Model B")).model_name, "Model B")
        self.assertFalse(self.catalog.benchmark_definitions.update(replace(definition, is_active=False)).is_active)
        self.assertFalse(self.catalog.prompt_templates.update(replace(prompt, is_active=False)).is_active)
        self.assertEqual(self.catalog.hardware_profiles.update(replace(hardware, gpu="RTX")).gpu, "RTX")
        for repository, item in ((self.catalog.model_profiles, model), (self.catalog.benchmark_definitions, definition), (self.catalog.prompt_templates, prompt), (self.catalog.hardware_profiles, hardware)):
            repository.delete(item.id)
            self.assertIsNone(repository.get(item.id))
