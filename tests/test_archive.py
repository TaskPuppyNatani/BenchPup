import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine.archive import (
    ARCHIVE_FORMAT,
    ArchiveError,
    ArchiveIssueCode,
    ArchiveReadError,
    ArchiveService,
    ArchiveValidationState,
    TABLES,
)
from engine.database import DatabaseMaintenanceError, EngineDatabase
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

    def empty_archive(self):
        archive = self.archive.build_archive("0.3")
        for table in TABLES:
            archive["data"][table] = []
            archive["counts"][table] = 0
        return archive

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

    def test_typed_inspection_exposes_fingerprint_counts_and_immutable_mappings(self):
        self.seed_all_entities()
        path = Path(self.directory.name) / "typed.json"
        result = self.archive.export_typed(path, "0.3")
        preview = self.archive.preview_typed(path)

        self.assertIs(preview.validation_state, ArchiveValidationState.VALID)
        self.assertTrue(preview.merge_eligible)
        self.assertTrue(preview.replace_eligible)
        self.assertEqual(preview.attachment_metadata_count, 1)
        self.assertEqual(preview.actual_counts, result.counts)
        self.assertEqual(preview.fingerprint.path, path.resolve())
        self.assertGreater(preview.fingerprint.size, 0)
        self.assertEqual(len(preview.fingerprint.sha256), 64)
        with self.assertRaises(TypeError):
            preview.actual_counts["benchmark_runs"] = 0

    def test_typed_preview_reports_decode_failure_without_writes(self):
        self.seed_all_entities()
        path = Path(self.directory.name) / "malformed.json"
        path.write_bytes(b"\xff\xfe{")
        before = self.service.runs.list()

        preview = self.archive.preview_typed(path)

        self.assertIs(preview.validation_state, ArchiveValidationState.INVALID)
        self.assertEqual(preview.issues[0].code, ArchiveIssueCode.MALFORMED_JSON)
        self.assertEqual(self.service.runs.list(), before)
        with self.assertRaises(ArchiveReadError) as raised:
            self.archive.load(path)
        self.assertEqual(raised.exception.code, ArchiveIssueCode.MALFORMED_JSON)

    def test_validation_catches_count_id_scalar_and_relationship_errors(self):
        self.seed_all_entities()
        valid = self.archive.build_archive("0.3")

        count_mismatch = json.loads(json.dumps(valid))
        count_mismatch["counts"]["benchmark_runs"] += 1
        self.assertIn(
            ArchiveIssueCode.COUNT_MISMATCH,
            {issue.code for issue in self.archive.inspect_archive(count_mismatch).issues},
        )

        duplicate_id = json.loads(json.dumps(valid))
        duplicate_id["data"]["model_profiles"].append(
            dict(duplicate_id["data"]["model_profiles"][0])
        )
        duplicate_id["counts"]["model_profiles"] += 1
        self.assertIn(
            ArchiveIssueCode.DUPLICATE_SOURCE_ID,
            {issue.code for issue in self.archive.inspect_archive(duplicate_id).issues},
        )

        scalar_error = json.loads(json.dumps(valid))
        scalar_error["data"]["model_profiles"][0]["name"] = []
        self.assertIn(
            ArchiveIssueCode.INVALID_RECORD,
            {issue.code for issue in self.archive.inspect_archive(scalar_error).issues},
        )

        relationship_error = json.loads(json.dumps(valid))
        relationship_error["data"]["review_scores"][0]["run_id"] = 999999
        self.assertIn(
            ArchiveIssueCode.INVALID_RELATIONSHIP,
            {
                issue.code
                for issue in self.archive.inspect_archive(relationship_error).issues
            },
        )

    def test_build_archive_uses_one_explicit_read_transaction(self):
        self.seed_all_entities()
        traces = []
        original_connect = self.database.connect

        def traced_connect(*, allow_during_maintenance=False):
            connection = original_connect(
                allow_during_maintenance=allow_during_maintenance
            )
            connection.set_trace_callback(traces.append)
            return connection

        with mock.patch.object(self.database, "connect", side_effect=traced_connect):
            self.archive.build_archive("0.3")
        self.assertTrue(any(statement.strip().upper() == "BEGIN" for statement in traces))

    def test_export_failure_preserves_existing_destination_and_cleans_temp(self):
        self.seed_all_entities()
        path = Path(self.directory.name) / "existing.json"
        self.archive.export(path, "0.3")
        original_bytes = path.read_bytes()

        with mock.patch(
            "engine.archive.os.replace",
            side_effect=PermissionError(13, "access denied"),
        ):
            with self.assertRaises(ArchiveError) as raised:
                self.archive.export(path, "0.3")

        self.assertEqual(raised.exception.code, ArchiveIssueCode.DESTINATION_LOCKED)
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(list(Path(self.directory.name).glob(".existing.json.*.tmp")), [])

    def test_merge_rolls_back_after_database_failure_during_iteration(self):
        archive = self.empty_archive()
        session = {
            "id": 1,
            "title": "same title",
            "description": "",
            "started_at": None,
            "completed_at": None,
            "notes": "",
            "created_at": "2026-07-22T00:00:00+00:00",
            "updated_at": "2026-07-22T00:00:00+00:00",
            "is_deleted": 0,
        }
        duplicate = dict(session)
        duplicate["id"] = 2
        duplicate["title"] = "second title"
        duplicate["is_deleted"] = 2
        archive["data"]["benchmark_sessions"] = [session, duplicate]
        archive["counts"]["benchmark_sessions"] = 2
        target_db = EngineDatabase(Path(self.directory.name) / "rollback.db")
        target_db.migrate()
        target = ArchiveService(target_db)

        with self.assertRaises(ArchiveError) as raised:
            target.merge(archive)

        self.assertEqual(raised.exception.code, ArchiveIssueCode.INTEGRITY_FAILED)
        with target_db.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM benchmark_sessions").fetchone()[0],
                0,
            )

    def test_merge_file_rejects_stale_archive_fingerprint(self):
        self.seed_all_entities()
        path = Path(self.directory.name) / "stale.json"
        self.archive.export(path, "0.3")
        fingerprint = self.archive.fingerprint(path)
        path.write_bytes(path.read_bytes() + b"\n")

        with self.assertRaises(ArchiveError) as raised:
            self.archive.merge_file(path, fingerprint=fingerprint)

        self.assertEqual(raised.exception.code, ArchiveIssueCode.STALE_ARCHIVE)

    def test_safety_backups_are_valid_and_never_reused(self):
        self.seed_all_entities()
        archive = self.archive.build_archive("0.3")
        first = self.archive.replace(archive, "0.3")
        second = self.archive.replace(archive, "0.3")

        self.assertNotEqual(first, second)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.archive.validate(self.archive.load(first))
        self.archive.validate(self.archive.load(second))

    def test_wal_is_checkpointed_before_replacement(self):
        self.seed_all_entities()
        archive = self.archive.build_archive("0.3")
        with self.database.connection() as connection:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        self.assertEqual(str(mode).casefold(), "wal")

        result = self.archive.replace_typed(archive, "0.3")

        self.assertTrue(result.safety_backup_path.exists())
        with self.database.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()[0],
                1,
            )

    def test_maintenance_quiesces_connections_and_allows_owner_verification(self):
        with self.database.maintenance(timeout=0.1):
            with self.database.connection(allow_during_maintenance=True) as connection:
                self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
            with self.assertRaises(DatabaseMaintenanceError):
                EngineDatabase(self.database.path).connect()

        connection = self.database.connect()
        try:
            with self.assertRaises(DatabaseMaintenanceError):
                with self.database.maintenance(timeout=0.01):
                    pass
        finally:
            connection.close()

    def test_merge_preserves_snapshot_and_attachment_metadata_verbatim(self):
        self.seed_all_entities()
        archive = self.archive.build_archive("0.3")
        target_db = EngineDatabase(Path(self.directory.name) / "snapshot-target.db")
        target_db.migrate()
        target = ArchiveService(target_db)

        target.merge(archive)

        expected_snapshot = archive["data"]["benchmark_runs"][0]["model_snapshot"]
        with target_db.connection() as connection:
            row = connection.execute(
                "SELECT model_snapshot, file_path, original_filename FROM benchmark_runs "
                "LEFT JOIN run_attachments ON run_attachments.run_id = benchmark_runs.id"
            ).fetchone()
        self.assertEqual(row["model_snapshot"], expected_snapshot)
        self.assertEqual(row["file_path"], "C:/log.txt")
        self.assertEqual(row["original_filename"], "log.txt")
