import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine.database import EngineDatabase
from engine.datasets import DatasetBuilder, DatasetValidation, DatasetWriteStatus
from engine.services import BenchmarkService


class DatasetWriteResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        database = EngineDatabase(Path(self.temp.name) / "test.db")
        database.migrate()
        self.builder = DatasetBuilder(
            BenchmarkService(database), benchpup_version="test", schema_version=5
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def path(self, name: str = "dataset.jsonl") -> Path:
        return Path(self.temp.name) / name

    def assert_no_temporary_files(self) -> None:
        self.assertEqual(list(Path(self.temp.name).glob("*.tmp")), [])

    def test_success_returns_paths_flags_message_and_sha(self) -> None:
        path = self.path()

        result = self.builder.write_dataset(path)

        self.assertEqual(result.status, DatasetWriteStatus.SUCCESS)
        self.assertIn("successfully", result.message)
        self.assertTrue(result.jsonl_finalized)
        self.assertTrue(result.manifest_finalized)
        self.assertTrue(result.cleanup_succeeded)
        self.assertTrue(path.exists())
        self.assertTrue(result.manifest_path.exists())
        self.assertEqual(len(result.sha256), 64)

    def test_existing_output_requires_overwrite_and_preserves_file(self) -> None:
        path = self.path()
        path.write_text("existing", encoding="utf-8")

        result = self.builder.write_dataset(path)

        self.assertEqual(result.status, DatasetWriteStatus.OVERWRITE_REQUIRED)
        self.assertEqual(path.read_text(encoding="utf-8"), "existing")
        self.assertFalse(result.jsonl_finalized)
        self.assertFalse(result.manifest_finalized)

    def test_overwrite_true_replaces_existing_outputs_after_validation(self) -> None:
        path = self.path()
        manifest = path.with_suffix(".jsonl.manifest.json")
        path.write_text("old dataset", encoding="utf-8")
        manifest.write_text("old manifest", encoding="utf-8")

        result = self.builder.write_dataset(path, overwrite=True)

        self.assertEqual(result.status, DatasetWriteStatus.SUCCESS)
        self.assertNotEqual(path.read_text(encoding="utf-8"), "old dataset")
        self.assertNotEqual(manifest.read_text(encoding="utf-8"), "old manifest")

    def test_jsonl_validation_failure_preserves_existing_files_and_cleans_temps(self) -> None:
        path = self.path()
        manifest = path.with_suffix(".jsonl.manifest.json")
        path.write_text("old dataset", encoding="utf-8")
        manifest.write_text("old manifest", encoding="utf-8")

        with patch.object(
            self.builder,
            "validate_dataset",
            return_value=DatasetValidation("validation_failed", message="bad JSONL"),
        ):
            result = self.builder.write_dataset(path, overwrite=True)

        self.assertEqual(result.status, DatasetWriteStatus.VALIDATION_FAILED)
        self.assertIn("bad JSONL", result.details)
        self.assertEqual(path.read_text(encoding="utf-8"), "old dataset")
        self.assertEqual(manifest.read_text(encoding="utf-8"), "old manifest")
        self.assertTrue(result.cleanup_succeeded)
        self.assert_no_temporary_files()

    def test_manifest_validation_failure_returns_validation_failed(self) -> None:
        path = self.path()

        with patch.object(
            self.builder,
            "validate_manifest",
            return_value=DatasetValidation("manifest_invalid", message="bad manifest"),
        ):
            result = self.builder.write_dataset(path)

        self.assertEqual(result.status, DatasetWriteStatus.VALIDATION_FAILED)
        self.assertIn("bad manifest", result.details)
        self.assertFalse(path.exists())
        self.assertFalse(result.manifest_path.exists())
        self.assert_no_temporary_files()

    def test_jsonl_temporary_write_failure_returns_temp_write_failed(self) -> None:
        path = self.path()

        with patch.object(Path, "open", side_effect=OSError("disk full")):
            result = self.builder.write_dataset(path)

        self.assertEqual(result.status, DatasetWriteStatus.TEMP_WRITE_FAILED)
        self.assertIn("disk full", result.details)
        self.assert_no_temporary_files()

    def test_manifest_temporary_write_failure_returns_temp_write_failed(self) -> None:
        path = self.path()

        with patch.object(Path, "write_text", side_effect=OSError("manifest disk full")):
            result = self.builder.write_dataset(path)

        self.assertEqual(result.status, DatasetWriteStatus.TEMP_WRITE_FAILED)
        self.assertIn("manifest disk full", result.details)
        self.assert_no_temporary_files()

    def test_jsonl_finalization_failure_reports_failed_path_and_cleans_temps(self) -> None:
        path = self.path()

        with patch("engine.datasets.os.replace", side_effect=OSError("rename denied")):
            result = self.builder.write_dataset(path)

        self.assertEqual(result.status, DatasetWriteStatus.JSONL_FINALIZE_FAILED)
        self.assertIn(str(path), result.details)
        self.assertFalse(result.jsonl_finalized)
        self.assertFalse(result.manifest_finalized)
        self.assertTrue(result.cleanup_succeeded)
        self.assertFalse(path.exists())
        self.assertFalse(result.manifest_path.exists())
        self.assert_no_temporary_files()

    def test_manifest_failure_after_jsonl_finalization_reports_partial_finalization(self) -> None:
        path = self.path()
        original_replace = os.replace
        calls = 0

        def replace_second_call(source: str | Path, destination: str | Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("manifest rename denied")
            original_replace(source, destination)

        with patch("engine.datasets.os.replace", side_effect=replace_second_call):
            result = self.builder.write_dataset(path)

        self.assertEqual(result.status, DatasetWriteStatus.PARTIAL_FINALIZATION)
        self.assertTrue(result.jsonl_finalized)
        self.assertFalse(result.manifest_finalized)
        self.assertIn(str(result.manifest_path), result.details)
        self.assertTrue(path.exists())
        self.assertFalse(result.manifest_path.exists())
        self.assert_no_temporary_files()

    def test_partial_finalization_preserves_existing_manifest_when_overwrite_is_allowed(self) -> None:
        path = self.path()
        manifest = path.with_suffix(".jsonl.manifest.json")
        path.write_text("old dataset", encoding="utf-8")
        manifest.write_text("old manifest", encoding="utf-8")
        original_replace = os.replace
        calls = 0

        def fail_manifest_replace(source: str | Path, destination: str | Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("manifest rename denied")
            original_replace(source, destination)

        with patch("engine.datasets.os.replace", side_effect=fail_manifest_replace):
            result = self.builder.write_dataset(path, overwrite=True)

        self.assertEqual(result.status, DatasetWriteStatus.PARTIAL_FINALIZATION)
        self.assertTrue(result.jsonl_finalized)
        self.assertEqual(manifest.read_text(encoding="utf-8"), "old manifest")

    def test_cleanup_failure_overrides_pre_finalization_failure_and_reports_remaining_temps(self) -> None:
        path = self.path()

        with patch.object(
            self.builder,
            "validate_dataset",
            return_value=DatasetValidation("validation_failed", message="bad JSONL"),
        ), patch.object(Path, "unlink", side_effect=OSError("cleanup denied")):
            result = self.builder.write_dataset(path)

        self.assertEqual(result.status, DatasetWriteStatus.TEMP_CLEANUP_FAILED)
        self.assertFalse(result.cleanup_succeeded)
        self.assertGreaterEqual(len(result.remaining_temp_paths), 1)
        self.assertIn("Original validation_failed", result.details)
        self.assertIn("Remaining temporary files", result.details)

    def test_cleanup_failure_does_not_hide_jsonl_finalization_failure(self) -> None:
        path = self.path()

        with patch("engine.datasets.os.replace", side_effect=OSError("rename denied")), patch.object(
            Path, "unlink", side_effect=OSError("cleanup denied")
        ):
            result = self.builder.write_dataset(path)

        self.assertEqual(result.status, DatasetWriteStatus.JSONL_FINALIZE_FAILED)
        self.assertFalse(result.cleanup_succeeded)
        self.assertGreaterEqual(len(result.remaining_temp_paths), 1)


if __name__ == "__main__":
    unittest.main()
