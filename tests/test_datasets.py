import hashlib
import json
import re
import sys
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine.database import EngineDatabase
from engine.datasets import (
    DATASET_FORMAT_VERSION,
    DATASET_VALIDATION_MAX_ISSUES,
    DatasetBuilder,
    DatasetFilters,
    DatasetValidationState,
    ManifestValidationState,
    PairValidationState,
    RedactionConfig,
    ValidationIssueCode,
)
from engine.domain import (
    BenchmarkDefinition,
    BenchmarkRun,
    BenchmarkSession,
    HardwareProfile,
    ModelProfile,
    PromptTemplate,
    ReviewScore,
    ScoreboardEntry,
)
from engine.services import BenchmarkService, CatalogService


class DatasetBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        database = EngineDatabase(self.root / "datasets.db")
        database.migrate()
        self.catalog = CatalogService(database)
        self.service = BenchmarkService(database, self.catalog)
        self.builder = DatasetBuilder(self.service, benchpup_version="test", schema_version=5)
        self.session = self.catalog.sessions.create(BenchmarkSession(title="Dataset session"))
        self.second_session = self.catalog.sessions.create(BenchmarkSession(title="Other session"))
        self.model = self.catalog.model_profiles.create(
            ModelProfile(name="Alpha profile", model_name="Alpha", backend="LM Studio", temperature=0.3)
        )
        self.definition = self.catalog.benchmark_definitions.create(
            BenchmarkDefinition(name="Review", file_path="review.py", benchmark_type="code_review")
        )
        self.template = self._template("Review prompt", "1")
        self.second_template = self._template("Other prompt", "2")
        self.hardware = self.catalog.hardware_profiles.create(HardwareProfile(name="Rig A"))
        self.second_hardware = self.catalog.hardware_profiles.create(HardwareProfile(name="Rig B"))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _template(self, text: str, version: str) -> PromptTemplate:
        return self.catalog.prompt_templates.create(
            PromptTemplate(
                name=f"Template {version}",
                version=version,
                prompt_text=text,
                prompt_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                benchmark_type="code_review",
            )
        )

    def make_run(
        self,
        *,
        output: str = "Model result",
        score_values: dict[str, object] | None = None,
        session_id: int | None = -1,
        prompt_template_id: int | None = -1,
        hardware_profile_id: int | None = -1,
        model_snapshot: dict[str, object] | None = None,
        benchmark_snapshot: dict[str, object] | None = None,
        prompt_snapshot: dict[str, object] | None = None,
        prompt_text: str = "",
        fingerprint: str = "",
        created_at: str = "2026-07-10T12:00:00+00:00",
        with_review: bool = True,
    ) -> BenchmarkRun:
        values: dict[str, object] = {
            "accuracy_score": 4.0,
            "hallucination_level": "Low",
            "reliability_level": "High",
            "depth_score": 4.0,
            "signal_noise_score": 4.0,
            "actionability_score": 4.0,
            "seniority_score": 4.0,
            "overall_score": 4.5,
            "verdict": "Approved",
        }
        values.update(score_values or {})
        run = BenchmarkRun(
            raw_model_output=output,
            session_id=self.session.id if session_id == -1 else session_id,
            model_profile_id=self.model.id,
            benchmark_definition_id=self.definition.id,
            prompt_template_id=self.template.id if prompt_template_id == -1 else prompt_template_id,
            hardware_profile_id=self.hardware.id if hardware_profile_id == -1 else hardware_profile_id,
            model_snapshot={} if model_snapshot is None else model_snapshot,
            benchmark_snapshot={} if benchmark_snapshot is None else benchmark_snapshot,
            prompt_snapshot={} if prompt_snapshot is None else prompt_snapshot,
            prompt_text=prompt_text,
            fingerprint=fingerprint,
            created_at=created_at,
        )
        saved, _ = self.service.save_run(
            run, ReviewScore(run_id=0, **values) if with_review else None
        )
        return saved

    def preview_ids(self, filters: DatasetFilters, runs: list[BenchmarkRun]) -> list[int]:
        return [record["metadata"]["source_run_id"] for record in self.builder.preview(runs, filters).records]

    def test_valid_run_builds_exact_jsonl_v1_record(self) -> None:
        run = self.make_run(output="Résumé: useful")

        records = self.builder.build_records([run])

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(set(record), {"instruction", "input", "response", "metadata"})
        self.assertEqual(record["input"]["raw_model_output"], "Résumé: useful")
        self.assertEqual(record["metadata"]["source_run_id"], run.id)
        self.assertEqual(record["metadata"]["format_version"], DATASET_FORMAT_VERSION)

    def test_eligibility_exclusion_codes(self) -> None:
        valid = self.make_run()
        deleted = self.make_run(output="deleted")
        self.service.delete_run(deleted.id)
        deleted = replace(deleted, is_deleted=True)
        no_output = self.make_run(output="   ")
        no_review = self.make_run(output="no review", with_review=False)
        missing_model = self.make_run(
            output="no model", model_snapshot={"backend": "LM Studio"}
        )
        missing_benchmark = self.make_run(
            output="no benchmark", benchmark_snapshot={"tags": "none"}
        )
        missing_prompt = self.make_run(
            output="no prompt", prompt_template_id=None, prompt_snapshot={"name": "empty"}
        )

        preview = self.builder.preview(
            [valid, deleted, no_output, no_review, missing_model, missing_benchmark, missing_prompt]
        )

        self.assertEqual(len(preview.records), 1)
        self.assertEqual(preview.excluded["soft_deleted"], 1)
        self.assertEqual(preview.excluded["missing_output"], 1)
        self.assertEqual(preview.excluded["missing_review"], 1)
        self.assertEqual(preview.excluded["missing_model_context"], 1)
        self.assertEqual(preview.excluded["missing_benchmark_context"], 1)
        self.assertEqual(preview.excluded["missing_prompt_context"], 1)

    def test_invalid_review_is_excluded(self) -> None:
        run = self.make_run()
        invalid = ReviewScore(run_id=run.id or 0, hallucination_level="Not a level")

        with patch.object(self.service, "get_run", return_value=(run, invalid, [])):
            preview = self.builder.preview([run])

        self.assertEqual(preview.excluded, {"invalid_review": 1})

    def test_missing_optional_metadata_is_warning_only(self) -> None:
        run = self.make_run(
            session_id=None,
            hardware_profile_id=None,
            model_snapshot={"model_name": "Alpha"},
            score_values={
                "accuracy_score": None,
                "depth_score": None,
                "signal_noise_score": None,
                "actionability_score": None,
                "seniority_score": None,
            },
        )

        preview = self.builder.preview([run])

        self.assertEqual(len(preview.records), 1)
        self.assertEqual(
            preview.warnings,
            {
                "missing_hardware": 1,
                "missing_session": 1,
                "missing_backend": 1,
                "missing_sampling": 1,
                "missing_optional_scores": 1,
            },
        )

    def test_all_filters_and_combined_filters(self) -> None:
        matching = self.make_run(created_at="2026-07-10T12:00:00+00:00")
        other = self.make_run(
            output="Other result",
            score_values={
                "overall_score": 2.0,
                "hallucination_level": "High",
                "reliability_level": "Low",
                "verdict": "Rejected",
            },
            session_id=self.second_session.id,
            prompt_template_id=self.second_template.id,
            hardware_profile_id=self.second_hardware.id,
            model_snapshot={"model_name": "Beta", "backend": "Ollama", "temperature": 0.8},
            benchmark_snapshot={
                "name": "Other benchmark",
                "file_path": "other.py",
                "benchmark_type": "code_generation",
            },
            created_at="2025-01-02T12:00:00+00:00",
        )
        match_id = matching.id
        self.assertIsNotNone(match_id)
        filters = (
            DatasetFilters(min_overall=4.0),
            DatasetFilters(max_hallucination="Medium"),
            DatasetFilters(min_reliability="Medium"),
            DatasetFilters(verdict="approved"),
            DatasetFilters(benchmark_type="code_review"),
            DatasetFilters(model="alpha"),
            DatasetFilters(session_id=self.session.id),
            DatasetFilters(date_from=date(2026, 1, 1), date_to=date(2026, 12, 31)),
            DatasetFilters(prompt_template_id=self.template.id),
            DatasetFilters(hardware_profile_id=self.hardware.id),
            DatasetFilters(include_run_ids=frozenset({match_id})),
            DatasetFilters(exclude_run_ids=frozenset({other.id or 0})),
            DatasetFilters(
                min_overall=4.0,
                max_hallucination="Medium",
                min_reliability="Medium",
                verdict="approved",
                benchmark_type="code_review",
                model="alpha",
                session_id=self.session.id,
                prompt_template_id=self.template.id,
                hardware_profile_id=self.hardware.id,
                date_from=date(2026, 1, 1),
                date_to=date(2026, 12, 31),
            ),
        )

        for filters_for_test in filters:
            with self.subTest(filters=filters_for_test):
                self.assertEqual(self.preview_ids(filters_for_test, [matching, other]), [match_id])

    def test_filters_report_filtered_out(self) -> None:
        run = self.make_run(score_values={"overall_score": 1.0})

        preview = self.builder.preview([run], DatasetFilters(min_overall=4.0))

        self.assertEqual(preview.excluded, {"filtered_out": 1})

    def test_source_duplicates_are_skipped_in_stable_run_id_order(self) -> None:
        first = self.make_run(output="Same output")
        second = replace(self.make_run(output="Different output"), raw_model_output="Same output")

        preview = self.builder.preview([second, first])

        self.assertEqual(preview.source_duplicates, 1)
        self.assertEqual([item["metadata"]["source_run_id"] for item in preview.records], [first.id])

    def test_source_duplicates_can_be_retained(self) -> None:
        first = self.make_run(output="Same output")
        second = replace(self.make_run(output="Different output"), raw_model_output="Same output")

        preview = self.builder.preview(
            [second, first], DatasetFilters(keep_source_duplicates=True)
        )

        self.assertEqual(preview.source_duplicates, 1)
        self.assertEqual([item["metadata"]["source_run_id"] for item in preview.records], [first.id, second.id])

    def test_fingerprint_and_near_duplicate_accounting(self) -> None:
        fingerprint_one = replace(self.make_run(output="First output"), fingerprint="same-fingerprint")
        fingerprint_two = replace(self.make_run(output="Second output"), fingerprint="same-fingerprint")
        near_one = self.make_run(output="Shared output", score_values={"verdict": "One"})
        near_two = replace(
            self.make_run(output="Different shared output", score_values={"verdict": "Two"}),
            raw_model_output="Shared output",
        )

        preview = self.builder.preview([near_two, fingerprint_two, near_one, fingerprint_one])

        self.assertEqual(preview.fingerprint_duplicates, 1)
        self.assertEqual(preview.near_duplicates, 1)
        self.assertEqual(len(preview.records), 4)

    def test_post_redaction_collisions_are_warnings_not_source_duplicates(self) -> None:
        first = self.make_run(output="user alice")
        second = self.make_run(output="user bob")
        config = RedactionConfig(
            literals=("alice", "bob"),
            redact_paths=False,
            redact_email=False,
            redact_hosts_ips=False,
        )

        preview = self.builder.preview([first, second], redaction_config=config)

        self.assertEqual(preview.source_duplicates, 0)
        self.assertEqual(preview.post_redaction_collisions, 1)
        self.assertEqual(len(preview.records), 2)

    def test_redaction_rules_and_source_record_immutability(self) -> None:
        source = "secret C:\\Users\\natan\\notes.txt username: natan natan@example.com host.example 192.0.2.1 token-42"
        run = self.make_run(output=source)
        original_output = run.raw_model_output
        original_snapshots = deepcopy(run.__dict__)
        config = RedactionConfig(
            literals=("secret",),
            redact_usernames=True,
            regex_patterns=(r"token-\d+",),
        )

        record = self.builder.build_records([run], redaction_config=config)[0]
        redacted = record["input"]["raw_model_output"]

        for token in (
            "[REDACTED_LITERAL]",
            "[REDACTED_PATH]",
            "[REDACTED_USERNAME]",
            "[REDACTED_EMAIL]",
            "[REDACTED_HOST]",
            "[REDACTED_CUSTOM]",
        ):
            self.assertIn(token, redacted)
        self.assertEqual(run.raw_model_output, original_output)
        self.assertEqual(run.__dict__, original_snapshots)

    def test_invalid_custom_redaction_regex_is_rejected(self) -> None:
        run = self.make_run()

        with self.assertRaises(re.error):
            self.builder.preview([run], redaction_config=RedactionConfig(regex_patterns=("[",)))

    def test_provenance_can_be_omitted(self) -> None:
        run = self.make_run()

        record = self.builder.build_records([run], DatasetFilters(include_provenance=False))[0]

        self.assertNotIn("source_run_id", record["metadata"])

    def test_scoreboard_entries_are_never_dataset_records(self) -> None:
        entry = ScoreboardEntry(model_name="Historical model", score=4.0)

        preview = self.builder.preview([entry])

        self.assertEqual(preview.records, [])
        self.assertEqual(preview.excluded, {"not_benchmark_run": 1})

    def test_validate_jsonl_accepts_utf8_blank_lines_and_exact_shape(self) -> None:
        path = self.root / "valid.jsonl"
        record = {"instruction": "Résumé", "input": {}, "response": {}, "metadata": {}}
        path.write_text("\n" + json.dumps(record, ensure_ascii=False) + "\n\n", encoding="utf-8")

        self.assertEqual(self.builder.validate_jsonl(path), 1)
        self.assertEqual(self.builder.validate_dataset(path).state, "success")

    def test_validate_jsonl_reports_line_numbers_and_missing_fields(self) -> None:
        malformed = self.root / "malformed.jsonl"
        valid = {"instruction": "x", "input": {}, "response": {}, "metadata": {}}
        malformed.write_text(json.dumps(valid) + "\n{bad}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Line 2"):
            self.builder.validate_jsonl(malformed)

        missing = self.root / "missing.jsonl"
        missing.write_text(json.dumps({"instruction": "x"}) + "\n", encoding="utf-8")
        validation = self.builder.validate_dataset(missing)
        self.assertEqual(validation.state, "validation_failed")
        self.assertIn("Line 1", validation.message)

    def test_structured_dataset_validation_counts_blank_lines_and_issues(self) -> None:
        path = self.root / "structured.jsonl"
        valid = {"instruction": "x", "input": {}, "response": {}, "metadata": {}}
        path.write_text(
            "\n" + json.dumps(valid) + "\n{bad}\n" + json.dumps({"instruction": "x"}) + "\n",
            encoding="utf-8",
        )

        result = self.builder.validate_dataset(path)

        self.assertIs(result.state, DatasetValidationState.INVALID)
        self.assertEqual(result.record_count, 1)
        self.assertEqual(result.nonblank_line_count, 3)
        self.assertEqual(result.blank_line_count, 1)
        self.assertEqual([issue.line_number for issue in result.issues], [3, 4])
        self.assertEqual(
            [issue.code for issue in result.issues],
            [ValidationIssueCode.MALFORMED_JSON, ValidationIssueCode.INVALID_RECORD_SHAPE],
        )
        self.assertFalse(result.issues_truncated)

    def test_structured_dataset_validation_limits_errors_deterministically(self) -> None:
        path = self.root / "many-errors.jsonl"
        path.write_text("{bad}\n" * (DATASET_VALIDATION_MAX_ISSUES + 5), encoding="utf-8")

        result = self.builder.validate_dataset(path)

        self.assertEqual(len(result.issues), DATASET_VALIDATION_MAX_ISSUES)
        self.assertTrue(result.issues_truncated)
        self.assertEqual(result.issues[0].line_number, 1)
        self.assertEqual(result.issues[-1].line_number, DATASET_VALIDATION_MAX_ISSUES)
        self.assertEqual(result.nonblank_line_count, DATASET_VALIDATION_MAX_ISSUES + 5)

    def test_structured_dataset_validation_preserves_empty_dataset_success(self) -> None:
        empty = self.root / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        result = self.builder.validate_dataset(empty)

        self.assertIs(result.state, DatasetValidationState.VALID)
        self.assertEqual(result.record_count, 0)
        self.assertEqual(result.nonblank_line_count, 0)
        self.assertEqual(result.blank_line_count, 0)
        self.assertEqual(result.issues, ())

    def test_structured_dataset_validation_distinguishes_missing_and_directory(self) -> None:
        missing = self.builder.validate_dataset(self.root / "not-found.jsonl")
        directory_path = self.root / "dataset-directory"
        directory_path.mkdir()
        directory = self.builder.validate_dataset(directory_path)

        self.assertIs(missing.state, DatasetValidationState.UNREADABLE)
        self.assertEqual(missing.issues[0].code, ValidationIssueCode.FILE_MISSING)
        self.assertIs(directory.state, DatasetValidationState.UNREADABLE)
        self.assertEqual(directory.issues[0].code, ValidationIssueCode.PATH_IS_DIRECTORY)

    def test_validate_jsonl_compatibility_preserves_count_and_first_error(self) -> None:
        valid_path = self.root / "compat-valid.jsonl"
        valid_path.write_text(
            json.dumps({"instruction": "x", "input": {}, "response": {}, "metadata": {}}) + "\n",
            encoding="utf-8",
        )
        invalid_path = self.root / "compat-invalid.jsonl"
        invalid_path.write_text("{bad}\n{still bad}\n", encoding="utf-8")

        self.assertEqual(self.builder.validate_jsonl(valid_path), 1)
        with self.assertRaisesRegex(ValueError, "Line 1"):
            self.builder.validate_jsonl(invalid_path)

    def test_structured_manifest_validation_exposes_metadata_and_types(self) -> None:
        dataset = self.root / "manifest-data.jsonl"
        dataset.write_text(
            json.dumps({"instruction": "x", "input": {}, "response": {}, "metadata": {}}) + "\n",
            encoding="utf-8",
        )
        manifest = self.root / "manifest-data.jsonl.manifest.json"
        manifest_data = {
            "dataset_filename": dataset.name,
            "record_count": 1,
            "excluded_count": 0,
            "duplicate_count": 0,
            "post_redaction_collisions": 0,
            "redaction_count": 0,
            "selected_filters": {"include_provenance": True},
            "benchpup_version": "test",
            "schema_version": 5,
            "created_at": "2026-07-10T00:00:00+00:00",
            "format_version": DATASET_FORMAT_VERSION,
            "sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
            "future_field": "accepted",
        }
        manifest.write_text(json.dumps(manifest_data), encoding="utf-8")

        result = self.builder.validate_manifest(manifest)

        self.assertIs(result.state, ManifestValidationState.VALID)
        self.assertEqual(result.declared_record_count, 1)
        self.assertEqual(result.declared_sha256, manifest_data["sha256"])
        self.assertEqual(result.schema_version, 5)
        self.assertEqual(result.format_version, DATASET_FORMAT_VERSION)
        self.assertIsNotNone(result.metadata)
        self.assertEqual(result.metadata["future_field"], "accepted")
        with self.assertRaises(TypeError):
            result.metadata["future_field"] = "changed"  # type: ignore[index]

    def test_structured_manifest_validation_reports_ordered_field_errors(self) -> None:
        manifest = self.root / "bad.manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "record_count": -1,
                    "excluded_count": "zero",
                    "duplicate_count": 0,
                    "redaction_count": 0,
                    "benchpup_version": "test",
                    "schema_version": 0,
                    "created_at": "not-a-timestamp",
                    "format_version": 99,
                    "sha256": "bad",
                    "selected_filters": [],
                }
            ),
            encoding="utf-8",
        )

        result = self.builder.validate_manifest(manifest)

        self.assertIs(result.state, ManifestValidationState.INVALID)
        self.assertEqual(result.issues[0].code, ValidationIssueCode.MISSING_REQUIRED_FIELD)
        self.assertEqual(result.issues[0].field, "dataset_filename")
        codes = {issue.code for issue in result.issues}
        self.assertIn(ValidationIssueCode.INVALID_RECORD_COUNT, codes)
        self.assertIn(ValidationIssueCode.INVALID_TIMESTAMP, codes)
        self.assertIn(ValidationIssueCode.UNSUPPORTED_FORMAT_VERSION, codes)
        self.assertIn(ValidationIssueCode.UNSUPPORTED_SCHEMA_VERSION, codes)
        self.assertIn(ValidationIssueCode.INVALID_SHA256, codes)
        self.assertIn(ValidationIssueCode.INVALID_FIELD_TYPE, codes)

    def test_structured_manifest_validation_rejects_malformed_and_non_object_roots(self) -> None:
        malformed = self.root / "malformed.manifest.json"
        malformed.write_text("{bad}", encoding="utf-8")
        non_object = self.root / "array.manifest.json"
        non_object.write_text("[]", encoding="utf-8")

        malformed_result = self.builder.validate_manifest(malformed)
        non_object_result = self.builder.validate_manifest(non_object)

        self.assertIs(malformed_result.state, ManifestValidationState.INVALID)
        self.assertEqual(malformed_result.issues[0].code, ValidationIssueCode.MALFORMED_JSON)
        self.assertIs(non_object_result.state, ManifestValidationState.INVALID)
        self.assertEqual(non_object_result.issues[0].code, ValidationIssueCode.ROOT_NOT_OBJECT)

    def test_manifest_validation_distinguishes_missing_and_directory(self) -> None:
        missing = self.builder.validate_manifest(self.root / "not-found.manifest.json")
        directory_path = self.root / "manifest-directory"
        directory_path.mkdir()
        directory = self.builder.validate_manifest(directory_path)

        self.assertIs(missing.state, ManifestValidationState.MISSING)
        self.assertEqual(missing.issues[0].code, ValidationIssueCode.FILE_MISSING)
        self.assertIs(directory.state, ManifestValidationState.UNREADABLE)
        self.assertEqual(directory.issues[0].code, ValidationIssueCode.PATH_IS_DIRECTORY)

    def test_pair_validation_exposes_nested_results_and_both_mismatches(self) -> None:
        dataset = self.root / "pair-structured.jsonl"
        dataset.write_text(
            json.dumps({"instruction": "x", "input": {}, "response": {}, "metadata": {}}) + "\n\n",
            encoding="utf-8",
        )
        original_dataset = dataset.read_bytes()
        manifest = self.root / "pair-structured.jsonl.manifest.json"
        manifest_data = {
            "dataset_filename": dataset.name,
            "record_count": 2,
            "excluded_count": 0,
            "duplicate_count": 0,
            "redaction_count": 0,
            "benchpup_version": "test",
            "schema_version": 5,
            "created_at": "2026-07-10T00:00:00+00:00",
            "format_version": DATASET_FORMAT_VERSION,
            "sha256": "0" * 64,
        }
        original_manifest = json.dumps(manifest_data).encode("utf-8")
        manifest.write_bytes(original_manifest)

        result = self.builder.verify_dataset_manifest_pair(dataset, manifest)

        self.assertIs(result.state, PairValidationState.INVALID)
        self.assertIs(result.dataset_result.state, DatasetValidationState.VALID)
        self.assertIs(result.manifest_result.state, ManifestValidationState.VALID)
        self.assertEqual(result.actual_record_count, 1)
        self.assertEqual(result.declared_record_count, 2)
        self.assertFalse(result.record_count_matches)
        self.assertEqual(result.actual_sha256, hashlib.sha256(original_dataset).hexdigest())
        self.assertEqual(result.declared_sha256, "0" * 64)
        self.assertFalse(result.sha256_matches)
        self.assertEqual(
            [issue.code for issue in result.issues],
            [ValidationIssueCode.RECORD_COUNT_MISMATCH, ValidationIssueCode.SHA256_MISMATCH],
        )
        self.assertEqual(dataset.read_bytes(), original_dataset)
        self.assertEqual(manifest.read_bytes(), original_manifest)

    def test_pair_validation_preserves_independent_invalid_results(self) -> None:
        dataset = self.root / "invalid-pair.jsonl"
        dataset.write_text("{bad}\n", encoding="utf-8")
        manifest = self.root / "invalid-pair.manifest.json"
        manifest.write_text(json.dumps({"record_count": 1}), encoding="utf-8")

        result = self.builder.verify_dataset_manifest_pair(dataset, manifest)

        self.assertIs(result.state, PairValidationState.INVALID)
        self.assertIs(result.dataset_result.state, DatasetValidationState.INVALID)
        self.assertIs(result.manifest_result.state, ManifestValidationState.INVALID)
        self.assertEqual(
            [issue.code for issue in result.issues],
            [ValidationIssueCode.DATASET_INVALID, ValidationIssueCode.MANIFEST_INVALID],
        )
        self.assertEqual(result.dataset_result.issues[0].line_number, 1)
        self.assertIsNone(result.record_count_matches)
        self.assertIsNone(result.sha256_matches)

    def test_validation_does_not_mutate_source_run_snapshots(self) -> None:
        run = self.make_run(
            model_snapshot={"model_name": "Alpha"},
            benchmark_snapshot={"name": "Review"},
            prompt_snapshot={"prompt_text": "Review"},
        )
        original = deepcopy(run.__dict__)
        dataset = self.root / "snapshot-check.jsonl"
        dataset.write_text("{bad}\n", encoding="utf-8")
        manifest = self.root / "snapshot-check.manifest.json"
        manifest.write_text("{bad}", encoding="utf-8")

        self.builder.verify_dataset_manifest_pair(dataset, manifest)

        self.assertEqual(run.__dict__, original)

    def test_manifest_and_pair_verification_detect_sha_and_count_errors(self) -> None:
        dataset = self.root / "pair.jsonl"
        record = {"instruction": "x", "input": {}, "response": {}, "metadata": {}}
        dataset.write_text(json.dumps(record) + "\n", encoding="utf-8")
        digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
        manifest = self.root / "pair.manifest.json"
        manifest_data = {
            "dataset_filename": dataset.name,
            "record_count": 1,
            "excluded_count": 0,
            "duplicate_count": 0,
            "redaction_count": 0,
            "benchpup_version": "test",
            "schema_version": 5,
            "created_at": "2026-07-10T00:00:00+00:00",
            "format_version": DATASET_FORMAT_VERSION,
            "sha256": digest,
        }
        manifest.write_text(json.dumps(manifest_data), encoding="utf-8")

        self.assertEqual(self.builder.validate_manifest(manifest).state, "success")
        matching_pair = self.builder.verify_dataset_manifest_pair(dataset, manifest)
        self.assertIs(matching_pair.state, PairValidationState.VALID)
        self.assertEqual(matching_pair.actual_record_count, 1)
        self.assertEqual(matching_pair.declared_record_count, 1)
        self.assertTrue(matching_pair.record_count_matches)
        self.assertEqual(matching_pair.actual_sha256, digest)
        self.assertEqual(matching_pair.declared_sha256, digest)
        self.assertTrue(matching_pair.sha256_matches)

        manifest_data["sha256"] = "0" * 64
        manifest.write_text(json.dumps(manifest_data), encoding="utf-8")
        self.assertIn("SHA-256", self.builder.verify_dataset_manifest_pair(dataset, manifest).message)

        manifest_data["sha256"] = digest
        manifest_data["record_count"] = 2
        manifest.write_text(json.dumps(manifest_data), encoding="utf-8")
        self.assertIn("record count", self.builder.verify_dataset_manifest_pair(dataset, manifest).message)

    def test_manifest_validation_rejects_missing_required_fields(self) -> None:
        manifest = self.root / "invalid.manifest.json"
        manifest.write_text(json.dumps({"record_count": 1}), encoding="utf-8")

        validation = self.builder.validate_manifest(manifest)

        self.assertEqual(validation.state, "manifest_invalid")
        self.assertIn("required fields", validation.message)


if __name__ == "__main__":
    unittest.main()
