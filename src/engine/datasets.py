"""Curated JSONL dataset generation from detailed BenchmarkRun records only."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .domain import LEVELS, BenchmarkRun, now
from .services import BenchmarkService

DATASET_FORMAT_VERSION = 1
DATASET_VALIDATION_MAX_ISSUES = 100
MANIFEST_REQUIRED_FIELDS = (
    "dataset_filename",
    "record_count",
    "excluded_count",
    "duplicate_count",
    "redaction_count",
    "benchpup_version",
    "schema_version",
    "created_at",
    "format_version",
    "sha256",
)


@dataclass(frozen=True)
class DatasetFilters:
    min_overall: float | None = None
    max_hallucination: str | None = None
    min_reliability: str | None = None
    verdict: str = ""
    benchmark_type: str = ""
    model: str = ""
    session_id: int | None = None
    prompt_template_id: int | None = None
    hardware_profile_id: int | None = None
    include_run_ids: frozenset[int] = frozenset()
    exclude_run_ids: frozenset[int] = frozenset()
    date_from: date | None = None
    date_to: date | None = None
    keep_source_duplicates: bool = False
    include_provenance: bool = True


@dataclass(frozen=True)
class RedactionConfig:
    literals: tuple[str, ...] = ()
    redact_paths: bool = True
    redact_usernames: bool = False
    redact_email: bool = True
    redact_hosts_ips: bool = True
    regex_patterns: tuple[str, ...] = ()


@dataclass
class DatasetPreview:
    records: list[dict[str, Any]] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    warnings: dict[str, int] = field(default_factory=dict)
    source_duplicates: int = 0
    fingerprint_duplicates: int = 0
    near_duplicates: int = 0
    post_redaction_collisions: int = 0
    redactions: int = 0
    redaction_counts: dict[str, int] = field(default_factory=dict)


class DatasetOutputError(RuntimeError):
    """Safe staged dataset finalization failed; final paths are reported clearly."""


class DatasetWriteStatus(str, Enum):
    SUCCESS = "success"; VALIDATION_FAILED = "validation_failed"; OVERWRITE_REQUIRED = "overwrite_required"
    TEMP_WRITE_FAILED = "temp_write_failed"; TEMP_CLEANUP_FAILED = "temp_cleanup_failed"
    JSONL_FINALIZE_FAILED = "jsonl_finalize_failed"
    PARTIAL_FINALIZATION = "partial_finalization"


@dataclass(frozen=True)
class DatasetWriteResult:
    status: DatasetWriteStatus
    jsonl_path: Path
    manifest_path: Path
    message: str = ""
    record_count: int = 0
    sha256: str = ""
    details: str = ""
    jsonl_finalized: bool = False
    manifest_finalized: bool = False
    cleanup_succeeded: bool = True
    remaining_temp_paths: tuple[Path, ...] = ()


class DatasetValidationState(str, Enum):
    """Typed dataset outcomes retaining the legacy success value."""

    VALID = "success"
    INVALID = "validation_failed"
    UNREADABLE = "unreadable"


class ManifestValidationState(str, Enum):
    """Typed manifest outcomes retaining legacy CLI state values."""

    VALID = "success"
    INVALID = "manifest_invalid"
    MISSING = "manifest_missing"
    UNREADABLE = "manifest_unreadable"


class PairValidationState(str, Enum):
    """Typed outcomes for independent validation plus pair comparison."""

    VALID = "success"
    INVALID = "pair_invalid"
    UNREADABLE = "pair_unreadable"


class ValidationIssueSource(str, Enum):
    DATASET = "dataset"
    MANIFEST = "manifest"
    PAIR = "pair"


class ValidationIssueCode(str, Enum):
    """Stable machine-readable validation issue identifiers."""

    FILE_MISSING = "file_missing"
    PATH_IS_DIRECTORY = "path_is_directory"
    READ_FAILED = "read_failed"
    MALFORMED_JSON = "malformed_json"
    INVALID_RECORD_SHAPE = "invalid_record_shape"
    BLANK_LINE = "blank_line"
    EMPTY_DATASET = "empty_dataset"
    ROOT_NOT_OBJECT = "root_not_object"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_FIELD_TYPE = "invalid_field_type"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    UNSUPPORTED_FORMAT_VERSION = "unsupported_format_version"
    INVALID_SHA256 = "invalid_sha256"
    INVALID_RECORD_COUNT = "invalid_record_count"
    INVALID_TIMESTAMP = "invalid_timestamp"
    DATASET_INVALID = "dataset_invalid"
    MANIFEST_INVALID = "manifest_invalid"
    SHA256_MISMATCH = "sha256_mismatch"
    RECORD_COUNT_MISMATCH = "record_count_mismatch"
    SCHEMA_VERSION_MISMATCH = "schema_version_mismatch"


@dataclass(frozen=True)
class ValidationIssue:
    """One deterministic, GUI-neutral validation issue."""

    source: ValidationIssueSource
    code: ValidationIssueCode
    message: str
    line_number: int | None = None
    field: str | None = None
    expected: str | int | float | bool | None = None
    actual: str | int | float | bool | None = None


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _freeze_manifest(value: Mapping[str, Any]) -> Mapping[str, Any]:
    frozen = _freeze_json_value(value)
    assert isinstance(frozen, Mapping)
    return frozen


def _first_issue_message(issues: tuple[ValidationIssue, ...]) -> str:
    if not issues:
        return ""
    if len(issues) > 1 and all(issue.code is ValidationIssueCode.MISSING_REQUIRED_FIELD for issue in issues):
        return "Manifest is missing required fields"
    assert issues
    return issues[0].message


@dataclass(frozen=True)
class DatasetFileValidation:
    """Structured validation for one UTF-8 JSONL dataset.

    Blank physical lines are accepted, counted, and do not invalidate the
    dataset. ``record_count`` counts successfully validated JSON records;
    ``nonblank_line_count`` counts every nonblank physical line.
    """

    state: DatasetValidationState
    path: Path
    record_count: int = 0
    nonblank_line_count: int = 0
    blank_line_count: int = 0
    issues: tuple[ValidationIssue, ...] = ()
    issues_truncated: bool = False
    shape_summary: str = "JSONL v1 records require instruction, input, response, and metadata"

    @property
    def message(self) -> str:
        return _first_issue_message(self.issues)

    @property
    def is_valid(self) -> bool:
        return self.state is DatasetValidationState.VALID


@dataclass(frozen=True)
class ManifestValidation:
    """Structured validation and metadata for a dataset manifest."""

    state: ManifestValidationState
    path: Path
    metadata: Mapping[str, Any] | None = None
    declared_record_count: int | None = None
    declared_sha256: str | None = None
    schema_version: int | None = None
    format_version: int | None = None
    created_at: str | None = None
    issues: tuple[ValidationIssue, ...] = ()
    issues_truncated: bool = False

    def __post_init__(self) -> None:
        if self.metadata is not None and not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", _freeze_manifest(self.metadata))

    @property
    def manifest(self) -> Mapping[str, Any] | None:
        """Compatibility alias for the previous DatasetValidation field."""

        return self.metadata

    @property
    def record_count(self) -> int:
        """Compatibility alias for the manifest-declared count."""

        return self.declared_record_count or 0

    @property
    def message(self) -> str:
        return _first_issue_message(self.issues)

    @property
    def is_valid(self) -> bool:
        return self.state is ManifestValidationState.VALID


@dataclass(frozen=True)
class DatasetManifestPairValidation:
    """Independent dataset/manifest results plus typed pair comparisons."""

    state: PairValidationState
    jsonl_path: Path
    manifest_path: Path
    dataset_result: DatasetFileValidation
    manifest_result: ManifestValidation
    actual_record_count: int | None = None
    declared_record_count: int | None = None
    record_count_matches: bool | None = None
    actual_sha256: str | None = None
    declared_sha256: str | None = None
    sha256_matches: bool | None = None
    schema_version_compatible: bool | None = None
    issues: tuple[ValidationIssue, ...] = ()
    issues_truncated: bool = False

    @property
    def pair_issues(self) -> tuple[ValidationIssue, ...]:
        return self.issues

    @property
    def record_count(self) -> int:
        """Compatibility alias for the actual count when available."""

        return self.actual_record_count or 0

    @property
    def manifest(self) -> Mapping[str, Any] | None:
        """Compatibility alias for the validated manifest metadata."""

        return self.manifest_result.metadata

    @property
    def message(self) -> str:
        if self.issues:
            return self.issues[0].message
        return self.dataset_result.message or self.manifest_result.message

    @property
    def is_valid(self) -> bool:
        return self.state is PairValidationState.VALID


@dataclass(frozen=True)
class DatasetValidation:
    """Compatibility wrapper retained for existing callers and test doubles.

    New validation callers should use DatasetFileValidation,
    ManifestValidation, or DatasetManifestPairValidation. The legacy
    ``state``, ``record_count``, ``message``, and ``manifest`` fields remain
    accepted by DatasetBuilder write-path callers.
    """

    state: str
    record_count: int = 0
    message: str = ""
    manifest: Mapping[str, Any] | None = None


class DatasetBuilder:
    def __init__(self, service: BenchmarkService, *, benchpup_version: str, schema_version: int):
        self.service, self.benchpup_version, self.schema_version = service, benchpup_version, schema_version

    def preview(
        self,
        runs: Any = None,
        filters: DatasetFilters | RedactionConfig = DatasetFilters(),
        redaction_config: RedactionConfig = RedactionConfig(),
    ) -> DatasetPreview:
        # Compatibility with the initial engine-only call shape: preview(filters, redaction).
        if isinstance(runs, DatasetFilters):
            active_filters = runs
            if isinstance(filters, RedactionConfig):
                redaction_config = filters
            active_runs = self.service.runs.list(include_deleted=True)
        else:
            active_filters = filters if isinstance(filters, DatasetFilters) else DatasetFilters()
            active_runs = self.service.runs.list(include_deleted=True) if runs is None else runs
        result, source_keys, fingerprints, prompt_outputs, exported_keys = DatasetPreview(), set(), set(), set(), set()
        ordered_runs = sorted(active_runs, key=lambda run: (not isinstance(run, BenchmarkRun), run.id is None if isinstance(run, BenchmarkRun) else True, run.id or 0 if isinstance(run, BenchmarkRun) else 0))
        for run in ordered_runs:
            if not isinstance(run, BenchmarkRun):
                result.excluded["not_benchmark_run"] = result.excluded.get("not_benchmark_run", 0) + 1
                continue
            reason, record = self._candidate(run, active_filters)
            if reason:
                result.excluded[reason] = result.excluded.get(reason, 0) + 1; continue
            assert record is not None
            for warning in self._warnings_for(run, record):
                result.warnings[warning] = result.warnings.get(warning, 0) + 1
            source_key = self._source_key(record)
            prompt_output_key = self._key({"prompt": record["input"]["prompt_text"], "output": record["input"]["raw_model_output"]})
            if prompt_output_key in prompt_outputs and source_key not in source_keys: result.near_duplicates += 1
            prompt_outputs.add(prompt_output_key)
            if run.fingerprint and run.fingerprint in fingerprints: result.fingerprint_duplicates += 1
            fingerprints.add(run.fingerprint)
            if source_key in source_keys:
                result.source_duplicates += 1
                if not active_filters.keep_source_duplicates: continue
            source_keys.add(source_key)
            redacted, count, rule_counts = self._redact(record, redaction_config)
            result.redactions += count
            for rule, rule_count in rule_counts.items():
                result.redaction_counts[rule] = result.redaction_counts.get(rule, 0) + rule_count
            exported_key = self._source_key(redacted)
            if exported_key in exported_keys: result.post_redaction_collisions += 1
            exported_keys.add(exported_key)
            result.records.append(redacted)
        return result

    def build_records(self, runs: Any = None, filters: DatasetFilters = DatasetFilters(), redaction_config: RedactionConfig = RedactionConfig()) -> list[dict[str, Any]]:
        return self.preview(runs, filters, redaction_config).records

    def _candidate(self, run: Any, filters: DatasetFilters) -> tuple[str | None, dict[str, Any] | None]:
        if run.is_deleted: return "soft_deleted", None
        if not run.raw_model_output.strip(): return "missing_output", None
        run_id = run.id
        if run_id is None: return "missing_review", None
        _, score, _ = self.service.get_run(run_id)
        if score is None: return "missing_review", None
        try:
            score.validate()
        except ValueError:
            return "invalid_review", None
        prompt = run.prompt_text or str(run.prompt_snapshot.get("prompt_text", ""))
        benchmark = run.benchmark_snapshot
        model = run.model_snapshot
        if not model.get("model_name"): return "missing_model_context", None
        if not (benchmark.get("name") or benchmark.get("file_path") or benchmark.get("benchmark_file")): return "missing_benchmark_context", None
        if not prompt: return "missing_prompt_context", None
        if filters.include_run_ids and run_id not in filters.include_run_ids: return "filtered_out", None
        if run_id in filters.exclude_run_ids: return "filtered_out", None
        if filters.min_overall is not None and (score.overall_score is None or score.overall_score < filters.min_overall): return "filtered_out", None
        rank = {level: number for number, level in enumerate(LEVELS)}
        if filters.max_hallucination and rank.get(score.hallucination_level, len(LEVELS)) > rank.get(filters.max_hallucination, len(LEVELS)): return "filtered_out", None
        if filters.min_reliability and rank.get(score.reliability_level, -1) < rank.get(filters.min_reliability, -1): return "filtered_out", None
        if filters.verdict and filters.verdict.casefold() not in score.verdict.casefold(): return "filtered_out", None
        if filters.benchmark_type and benchmark.get("benchmark_type") != filters.benchmark_type: return "filtered_out", None
        if filters.model and filters.model.casefold() not in str(model.get("model_name", "")).casefold(): return "filtered_out", None
        if filters.session_id is not None and run.session_id != filters.session_id: return "filtered_out", None
        if filters.prompt_template_id is not None and run.prompt_template_id != filters.prompt_template_id: return "filtered_out", None
        if filters.hardware_profile_id is not None and run.hardware_profile_id != filters.hardware_profile_id: return "filtered_out", None
        if filters.date_from and run.created_at[:10] < filters.date_from.isoformat(): return "filtered_out", None
        if filters.date_to and run.created_at[:10] > filters.date_to.isoformat(): return "filtered_out", None
        metadata: dict[str, Any] = {"backend": model.get("backend", ""), "sampling": {key: model.get(key) for key in ("temperature", "top_p", "top_k", "min_p")}, "tokens_per_second": model.get("tokens_per_second"), "hardware": run.hardware_snapshot, "recorded_at": run.created_at, "benchpup_version": self.benchpup_version, "schema_version": self.schema_version, "format_version": DATASET_FORMAT_VERSION}
        if filters.include_provenance: metadata["source_run_id"] = run_id
        return None, {"instruction": "Evaluate the following benchmark result.", "input": {"model": model, "benchmark": benchmark, "prompt_template": run.prompt_snapshot, "prompt_text": prompt, "raw_model_output": run.raw_model_output}, "response": {"accuracy": score.accuracy_score, "hallucination": score.hallucination_level, "reliability": score.reliability_level, "depth": score.depth_score, "signal_to_noise": score.signal_noise_score, "actionability": score.actionability_score, "seniority": score.seniority_score, "overall": score.overall_score, "strengths": score.strengths, "weaknesses": score.weaknesses, "verdict": score.verdict, "notes": score.notes}, "metadata": metadata}

    @staticmethod
    def _warnings_for(run: BenchmarkRun, record: dict[str, Any]) -> tuple[str, ...]:
        model = record["input"]["model"]
        sampling = record["metadata"]["sampling"]
        response = record["response"]
        warnings: list[str] = []
        if not run.hardware_snapshot:
            warnings.append("missing_hardware")
        if run.session_id is None:
            warnings.append("missing_session")
        if not model.get("backend"):
            warnings.append("missing_backend")
        if not any(value is not None for value in sampling.values()):
            warnings.append("missing_sampling")
        optional_scores = ("accuracy", "depth", "signal_to_noise", "actionability", "seniority")
        if any(response[name] is None for name in optional_scores):
            warnings.append("missing_optional_scores")
        return tuple(warnings)

    @staticmethod
    def _key(record: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _source_key(record: dict[str, Any]) -> str:
        """Duplicate key excludes local provenance and non-content metadata."""
        return DatasetBuilder._key({"input": record["input"], "response": record["response"]})

    def _redact(self, record: dict[str, Any], config: RedactionConfig) -> tuple[dict[str, Any], int, dict[str, int]]:
        text = json.dumps(record, ensure_ascii=False); count = 0
        rule_counts: dict[str, int] = {}
        rules: list[tuple[str, str, str]] = [("literal", re.escape(value), "[REDACTED_LITERAL]") for value in config.literals if value]
        if config.redact_email: rules.append(("email", r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]"))
        if config.redact_paths: rules.append(("path", r"(?:[A-Za-z]:\\|/)[^\"\s]+", "[REDACTED_PATH]"))
        if config.redact_usernames: rules.append(("username", r"\b(?:user(?:name)?|login)\s*[:=]\s*[^\s,\"]+", "[REDACTED_USERNAME]"))
        if config.redact_hosts_ips: rules.append(("host_ip", r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b", "[REDACTED_HOST]"))
        rules += [("custom", pattern, "[REDACTED_CUSTOM]") for pattern in config.regex_patterns]
        for name, pattern, replacement in rules:
            text, changed = re.subn(pattern, replacement, text); count += changed
            rule_counts[name] = rule_counts.get(name, 0) + changed
        return json.loads(text), count, rule_counts

    def write(self, destination: str | Path, preview: DatasetPreview, filters: DatasetFilters) -> tuple[Path, Path]:
        path = Path(destination); manifest = path.with_suffix(path.suffix + ".manifest.json")
        if path.exists() or manifest.exists(): raise FileExistsError("Dataset or manifest already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=path.parent) as temporary:
            jsonl_temp, manifest_temp = Path(temporary) / path.name, Path(temporary) / manifest.name
            with jsonl_temp.open("w", encoding="utf-8", newline="\n") as output:
                for record in preview.records:
                    line = json.dumps(record, ensure_ascii=False); json.loads(line); output.write(line + "\n")
            digest = hashlib.sha256(jsonl_temp.read_bytes()).hexdigest()
            data = {"dataset_filename": path.name, "record_count": len(preview.records), "excluded_count": sum(preview.excluded.values()), "duplicate_count": preview.source_duplicates, "post_redaction_collisions": preview.post_redaction_collisions, "redaction_count": preview.redactions, "selected_filters": filters.__dict__, "benchpup_version": self.benchpup_version, "schema_version": self.schema_version, "created_at": now(), "format_version": DATASET_FORMAT_VERSION, "sha256": digest}
            manifest_temp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8"); json.loads(manifest_temp.read_text(encoding="utf-8"))
            try:
                os.replace(jsonl_temp, path)
                os.replace(manifest_temp, manifest)
            except OSError as error:
                state = "dataset final file was replaced" if path.exists() else "no final file was replaced"
                raise DatasetOutputError(f"Staged finalization failed; {state}: {error}") from error
        return path, manifest

    def write_dataset(
        self,
        destination: str | Path,
        runs: Any = None,
        filters: DatasetFilters = DatasetFilters(),
        redaction_config: RedactionConfig = RedactionConfig(),
        *,
        overwrite: bool = False,
    ) -> DatasetWriteResult:
        preview = self.preview(runs, filters, redaction_config)
        path = Path(destination)
        manifest = path.with_suffix(path.suffix + ".manifest.json")
        if not overwrite and (path.exists() or manifest.exists()):
            return DatasetWriteResult(
                DatasetWriteStatus.OVERWRITE_REQUIRED,
                path,
                manifest,
                "Existing output requires explicit overwrite confirmation",
                details=f"Existing output: {path if path.exists() else manifest}",
            )

        temporary_paths: list[Path] = []
        result: DatasetWriteResult
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            jsonl_temp = self._temporary_path(path)
            manifest_temp = self._temporary_path(manifest)
            temporary_paths = [jsonl_temp, manifest_temp]
            with jsonl_temp.open("w", encoding="utf-8", newline="\n") as output:
                for record in preview.records:
                    output.write(json.dumps(record, ensure_ascii=False) + "\n")
            dataset_validation = self.validate_dataset(jsonl_temp)
            if dataset_validation.state != "success":
                result = DatasetWriteResult(
                    DatasetWriteStatus.VALIDATION_FAILED,
                    path,
                    manifest,
                    "Temporary JSONL validation failed",
                    details=dataset_validation.message,
                )
                return self._with_cleanup_result(result, temporary_paths)
            digest = hashlib.sha256(jsonl_temp.read_bytes()).hexdigest()
            manifest_data = {
                "dataset_filename": path.name,
                "record_count": len(preview.records),
                "excluded_count": sum(preview.excluded.values()),
                "duplicate_count": preview.source_duplicates,
                "post_redaction_collisions": preview.post_redaction_collisions,
                "redaction_count": preview.redactions,
                "selected_filters": filters.__dict__,
                "benchpup_version": self.benchpup_version,
                "schema_version": self.schema_version,
                "created_at": now(),
                "format_version": DATASET_FORMAT_VERSION,
                "sha256": digest,
            }
            manifest_temp.write_text(json.dumps(manifest_data, indent=2, default=str), encoding="utf-8")
            manifest_validation = self.validate_manifest(manifest_temp)
            if manifest_validation.state != "success":
                result = DatasetWriteResult(
                    DatasetWriteStatus.VALIDATION_FAILED,
                    path,
                    manifest,
                    "Temporary manifest validation failed",
                    details=manifest_validation.message,
                )
                return self._with_cleanup_result(result, temporary_paths)
            try:
                os.replace(jsonl_temp, path)
            except OSError as error:
                result = DatasetWriteResult(
                    DatasetWriteStatus.JSONL_FINALIZE_FAILED,
                    path,
                    manifest,
                    "Could not finalize the dataset file",
                    details=f"{path}: {error}",
                )
                return self._with_cleanup_result(result, temporary_paths)
            try:
                os.replace(manifest_temp, manifest)
            except OSError as error:
                result = DatasetWriteResult(
                    DatasetWriteStatus.PARTIAL_FINALIZATION,
                    path,
                    manifest,
                    "Dataset file finalized but manifest finalization failed",
                    record_count=len(preview.records),
                    sha256=digest,
                    details=f"{manifest}: {error}",
                    jsonl_finalized=True,
                )
                return self._with_cleanup_result(result, temporary_paths)
            return DatasetWriteResult(
                DatasetWriteStatus.SUCCESS,
                path,
                manifest,
                "Dataset and manifest finalized successfully",
                record_count=len(preview.records),
                sha256=digest,
                jsonl_finalized=True,
                manifest_finalized=True,
            )
        except OSError as error:
            result = DatasetWriteResult(
                DatasetWriteStatus.TEMP_WRITE_FAILED,
                path,
                manifest,
                "Could not write temporary dataset output",
                details=f"{type(error).__name__}: {error}",
            )
            return self._with_cleanup_result(result, temporary_paths)

    @staticmethod
    def _temporary_path(final_path: Path) -> Path:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{final_path.name}.", suffix=".tmp", dir=final_path.parent
        )
        os.close(descriptor)
        return Path(name)

    @staticmethod
    def _cleanup_temporary_files(paths: list[Path]) -> tuple[Path, ...]:
        remaining: list[Path] = []
        for temporary in paths:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                remaining.append(temporary)
        return tuple(remaining)

    def _with_cleanup_result(
        self, result: DatasetWriteResult, temporary_paths: list[Path]
    ) -> DatasetWriteResult:
        remaining = self._cleanup_temporary_files(temporary_paths)
        if not remaining:
            return result
        cleanup_details = "Remaining temporary files: " + ", ".join(str(path) for path in remaining)
        details = f"{result.details}; {cleanup_details}" if result.details else cleanup_details
        # A partially finalized pair is the most important state to surface. For
        # all other pre-finalization failures, cleanup failure becomes the result.
        if result.status in {
            DatasetWriteStatus.JSONL_FINALIZE_FAILED,
            DatasetWriteStatus.PARTIAL_FINALIZATION,
        }:
            return replace(result, details=details, cleanup_succeeded=False, remaining_temp_paths=remaining)
        return DatasetWriteResult(
            DatasetWriteStatus.TEMP_CLEANUP_FAILED,
            result.jsonl_path,
            result.manifest_path,
            "Temporary output cleanup failed",
            record_count=result.record_count,
            sha256=result.sha256,
            details=f"Original {result.status.value}: {details}",
            jsonl_finalized=result.jsonl_finalized,
            manifest_finalized=result.manifest_finalized,
            cleanup_succeeded=False,
            remaining_temp_paths=remaining,
        )

    @staticmethod
    def _validate_jsonl_structured(path: str | Path) -> DatasetFileValidation:
        dataset_path = Path(path)
        issues: list[ValidationIssue] = []
        issues_truncated = False

        def add_issue(issue: ValidationIssue) -> None:
            nonlocal issues_truncated
            if len(issues) < DATASET_VALIDATION_MAX_ISSUES:
                issues.append(issue)
            else:
                issues_truncated = True

        record_count = 0
        nonblank_line_count = 0
        blank_line_count = 0
        try:
            is_directory = dataset_path.is_dir()
        except OSError:
            is_directory = False
        if is_directory:
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.DATASET,
                    ValidationIssueCode.PATH_IS_DIRECTORY,
                    "Dataset path is a directory, not a file",
                    expected="file",
                    actual=str(dataset_path),
                )
            )
            return DatasetFileValidation(
                DatasetValidationState.UNREADABLE,
                dataset_path,
                issues=tuple(issues),
                issues_truncated=issues_truncated,
            )
        try:
            with dataset_path.open("r", encoding="utf-8", newline="") as source:
                for number, line in enumerate(source, start=1):
                    if not line.strip():
                        blank_line_count += 1
                        continue
                    nonblank_line_count += 1
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as error:
                        add_issue(
                            ValidationIssue(
                                ValidationIssueSource.DATASET,
                                ValidationIssueCode.MALFORMED_JSON,
                                f"Line {number} contains invalid JSON: {error.msg}",
                                line_number=number,
                                expected="JSON object",
                                actual="malformed JSON",
                            )
                        )
                        continue
                    if not isinstance(record, dict) or set(record) != {"instruction", "input", "response", "metadata"}:
                        add_issue(
                            ValidationIssue(
                                ValidationIssueSource.DATASET,
                                ValidationIssueCode.INVALID_RECORD_SHAPE,
                                f"Line {number} does not match the JSONL v1 contract",
                                line_number=number,
                                expected="instruction, input, response, metadata",
                                actual=type(record).__name__,
                            )
                        )
                        continue
                    record_count += 1
        except FileNotFoundError:
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.DATASET,
                    ValidationIssueCode.FILE_MISSING,
                    "Dataset file was not found",
                    expected="existing file",
                    actual=str(dataset_path),
                )
            )
            return DatasetFileValidation(
                DatasetValidationState.UNREADABLE,
                dataset_path,
                record_count,
                nonblank_line_count,
                blank_line_count,
                tuple(issues),
                issues_truncated,
            )
        except IsADirectoryError:
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.DATASET,
                    ValidationIssueCode.PATH_IS_DIRECTORY,
                    "Dataset path is a directory, not a file",
                    expected="file",
                    actual=str(dataset_path),
                )
            )
            return DatasetFileValidation(
                DatasetValidationState.UNREADABLE,
                dataset_path,
                record_count,
                nonblank_line_count,
                blank_line_count,
                tuple(issues),
                issues_truncated,
            )
        except UnicodeDecodeError:
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.DATASET,
                    ValidationIssueCode.READ_FAILED,
                    "Dataset file could not be decoded as UTF-8",
                    expected="UTF-8 text",
                    actual="decode failure",
                )
            )
            return DatasetFileValidation(
                DatasetValidationState.UNREADABLE,
                dataset_path,
                record_count,
                nonblank_line_count,
                blank_line_count,
                tuple(issues),
                issues_truncated,
            )
        except OSError:
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.DATASET,
                    ValidationIssueCode.READ_FAILED,
                    "Dataset file could not be read",
                    expected="readable file",
                    actual=str(dataset_path),
                )
            )
            return DatasetFileValidation(
                DatasetValidationState.UNREADABLE,
                dataset_path,
                record_count,
                nonblank_line_count,
                blank_line_count,
                tuple(issues),
                issues_truncated,
            )

        state = DatasetValidationState.INVALID if issues else DatasetValidationState.VALID
        return DatasetFileValidation(
            state,
            dataset_path,
            record_count,
            nonblank_line_count,
            blank_line_count,
            tuple(issues),
            issues_truncated,
        )

    @staticmethod
    def _compatibility_validation_error(result: DatasetFileValidation) -> Exception:
        issue = result.issues[0] if result.issues else None
        if issue is not None and issue.code is ValidationIssueCode.FILE_MISSING:
            return FileNotFoundError(str(result.path))
        if issue is not None and issue.code is ValidationIssueCode.PATH_IS_DIRECTORY:
            return IsADirectoryError(str(result.path))
        if issue is not None and issue.code is ValidationIssueCode.READ_FAILED:
            return OSError(issue.message)
        return ValueError(result.message or "Dataset validation failed")

    @staticmethod
    def validate_jsonl(path: str | Path) -> int:
        """Return the legacy count or raise the first compatible validation error."""

        result = DatasetBuilder._validate_jsonl_structured(path)
        if result.state is DatasetValidationState.VALID:
            return result.record_count
        raise DatasetBuilder._compatibility_validation_error(result)

    def validate_dataset(self, path: str | Path) -> DatasetFileValidation:
        """Return complete structured JSONL validation without changing the file."""

        return self._validate_jsonl_structured(path)

    @staticmethod
    def validate_manifest(path: str | Path) -> ManifestValidation:
        """Validate the writer-produced manifest contract without rewriting it."""

        manifest_path = Path(path)
        issues: list[ValidationIssue] = []
        issues_truncated = False

        def add_issue(issue: ValidationIssue) -> None:
            nonlocal issues_truncated
            if len(issues) < DATASET_VALIDATION_MAX_ISSUES:
                issues.append(issue)
            else:
                issues_truncated = True

        try:
            is_directory = manifest_path.is_dir()
        except OSError:
            is_directory = False
        if is_directory:
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.MANIFEST,
                    ValidationIssueCode.PATH_IS_DIRECTORY,
                    "Manifest path is a directory, not a file",
                    expected="file",
                    actual=str(manifest_path),
                )
            )
            return ManifestValidation(
                ManifestValidationState.UNREADABLE,
                manifest_path,
                issues=tuple(issues),
                issues_truncated=issues_truncated,
            )

        try:
            raw = manifest_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.MANIFEST,
                    ValidationIssueCode.FILE_MISSING,
                    "Companion manifest was not found",
                    expected="existing file",
                    actual=str(manifest_path),
                )
            )
            return ManifestValidation(
                ManifestValidationState.MISSING,
                manifest_path,
                issues=tuple(issues),
                issues_truncated=issues_truncated,
            )
        except IsADirectoryError:
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.MANIFEST,
                    ValidationIssueCode.PATH_IS_DIRECTORY,
                    "Manifest path is a directory, not a file",
                    expected="file",
                    actual=str(manifest_path),
                )
            )
            return ManifestValidation(
                ManifestValidationState.UNREADABLE,
                manifest_path,
                issues=tuple(issues),
                issues_truncated=issues_truncated,
            )
        except UnicodeDecodeError:
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.MANIFEST,
                    ValidationIssueCode.READ_FAILED,
                    "Manifest file could not be decoded as UTF-8",
                    expected="UTF-8 text",
                    actual="decode failure",
                )
            )
            return ManifestValidation(
                ManifestValidationState.UNREADABLE,
                manifest_path,
                issues=tuple(issues),
                issues_truncated=issues_truncated,
            )
        except OSError:
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.MANIFEST,
                    ValidationIssueCode.READ_FAILED,
                    "Manifest file could not be read",
                    expected="readable file",
                    actual=str(manifest_path),
                )
            )
            return ManifestValidation(
                ManifestValidationState.UNREADABLE,
                manifest_path,
                issues=tuple(issues),
                issues_truncated=issues_truncated,
            )

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.MANIFEST,
                    ValidationIssueCode.MALFORMED_JSON,
                    f"Manifest contains invalid JSON: {error.msg}",
                    line_number=error.lineno,
                    expected="JSON object",
                    actual="malformed JSON",
                )
            )
            return ManifestValidation(
                ManifestValidationState.INVALID,
                manifest_path,
                issues=tuple(issues),
                issues_truncated=issues_truncated,
            )

        if not isinstance(data, dict):
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.MANIFEST,
                    ValidationIssueCode.ROOT_NOT_OBJECT,
                    "Manifest root must be a JSON object",
                    expected="object",
                    actual=type(data).__name__,
                )
            )
            return ManifestValidation(
                ManifestValidationState.INVALID,
                manifest_path,
                issues=tuple(issues),
                issues_truncated=issues_truncated,
            )

        for field_name in MANIFEST_REQUIRED_FIELDS:
            if field_name not in data:
                add_issue(
                    ValidationIssue(
                        ValidationIssueSource.MANIFEST,
                        ValidationIssueCode.MISSING_REQUIRED_FIELD,
                        f"Manifest is missing required field '{field_name}'",
                        field=field_name,
                    )
                )

        def is_nonnegative_integer(value: Any) -> bool:
            return isinstance(value, int) and not isinstance(value, bool) and value >= 0

        for field_name in ("record_count", "excluded_count", "duplicate_count", "redaction_count"):
            if field_name not in data:
                continue
            value = data[field_name]
            if not is_nonnegative_integer(value):
                code = ValidationIssueCode.INVALID_RECORD_COUNT if field_name == "record_count" else ValidationIssueCode.INVALID_FIELD_TYPE
                add_issue(
                    ValidationIssue(
                        ValidationIssueSource.MANIFEST,
                        code,
                        f"Manifest field '{field_name}' must be a non-negative integer",
                        field=field_name,
                        expected="non-negative integer",
                        actual=type(value).__name__ if not isinstance(value, int) else value,
                    )
                )

        if "post_redaction_collisions" in data and not is_nonnegative_integer(data["post_redaction_collisions"]):
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.MANIFEST,
                    ValidationIssueCode.INVALID_FIELD_TYPE,
                    "Manifest field 'post_redaction_collisions' must be a non-negative integer",
                    field="post_redaction_collisions",
                    expected="non-negative integer",
                    actual=type(data["post_redaction_collisions"]).__name__ if not isinstance(data["post_redaction_collisions"], int) else data["post_redaction_collisions"],
                )
            )

        for field_name in ("dataset_filename", "benchpup_version"):
            if field_name not in data:
                continue
            value = data[field_name]
            if not isinstance(value, str) or not value.strip():
                add_issue(
                    ValidationIssue(
                        ValidationIssueSource.MANIFEST,
                        ValidationIssueCode.INVALID_FIELD_TYPE,
                        f"Manifest field '{field_name}' must be a non-empty string",
                        field=field_name,
                        expected="non-empty string",
                        actual=type(value).__name__ if not isinstance(value, str) else value,
                    )
                )

        if "schema_version" in data:
            schema_version = data["schema_version"]
            if not is_nonnegative_integer(schema_version):
                add_issue(
                    ValidationIssue(
                        ValidationIssueSource.MANIFEST,
                        ValidationIssueCode.INVALID_FIELD_TYPE,
                        "Manifest field 'schema_version' must be a non-negative integer",
                        field="schema_version",
                        expected="non-negative integer",
                        actual=type(schema_version).__name__ if not isinstance(schema_version, int) else schema_version,
                    )
                )
            elif schema_version < 1:
                add_issue(
                    ValidationIssue(
                        ValidationIssueSource.MANIFEST,
                        ValidationIssueCode.UNSUPPORTED_SCHEMA_VERSION,
                        "Manifest schema version is not supported",
                        field="schema_version",
                        expected="positive schema version",
                        actual=schema_version,
                    )
                )

        if "format_version" in data:
            format_version = data["format_version"]
            if not isinstance(format_version, int) or isinstance(format_version, bool):
                add_issue(
                    ValidationIssue(
                        ValidationIssueSource.MANIFEST,
                        ValidationIssueCode.INVALID_FIELD_TYPE,
                        "Manifest field 'format_version' must be an integer",
                        field="format_version",
                        expected="integer",
                        actual=type(format_version).__name__ if not isinstance(format_version, int) else format_version,
                    )
                )
            elif format_version != DATASET_FORMAT_VERSION:
                add_issue(
                    ValidationIssue(
                        ValidationIssueSource.MANIFEST,
                        ValidationIssueCode.UNSUPPORTED_FORMAT_VERSION,
                        "Manifest format version is not supported",
                        field="format_version",
                        expected=DATASET_FORMAT_VERSION,
                        actual=format_version,
                    )
                )

        if "created_at" in data:
            created_at = data["created_at"]
            if not isinstance(created_at, str):
                add_issue(
                    ValidationIssue(
                        ValidationIssueSource.MANIFEST,
                        ValidationIssueCode.INVALID_FIELD_TYPE,
                        "Manifest field 'created_at' must be an ISO-8601 timestamp",
                        field="created_at",
                        expected="ISO-8601 timestamp",
                        actual=type(created_at).__name__,
                    )
                )
            else:
                try:
                    parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    if parsed.tzinfo is None or parsed.utcoffset() is None:
                        raise ValueError
                except ValueError:
                    add_issue(
                        ValidationIssue(
                            ValidationIssueSource.MANIFEST,
                            ValidationIssueCode.INVALID_TIMESTAMP,
                            "Manifest field 'created_at' must be a timezone-aware ISO-8601 timestamp",
                            field="created_at",
                            expected="timezone-aware ISO-8601 timestamp",
                            actual=created_at,
                        )
                    )

        if "sha256" in data:
            sha256 = data["sha256"]
            if not isinstance(sha256, str):
                add_issue(
                    ValidationIssue(
                        ValidationIssueSource.MANIFEST,
                        ValidationIssueCode.INVALID_FIELD_TYPE,
                        "Manifest field 'sha256' must be a 64-character hexadecimal string",
                        field="sha256",
                        expected="64-character hexadecimal string",
                        actual=type(sha256).__name__,
                    )
                )
            elif not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
                add_issue(
                    ValidationIssue(
                        ValidationIssueSource.MANIFEST,
                        ValidationIssueCode.INVALID_SHA256,
                        "Manifest field 'sha256' is not a valid SHA-256 digest",
                        field="sha256",
                        expected="64 hexadecimal characters",
                        actual=sha256,
                    )
                )

        if "selected_filters" in data and not isinstance(data["selected_filters"], dict):
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.MANIFEST,
                    ValidationIssueCode.INVALID_FIELD_TYPE,
                    "Manifest field 'selected_filters' must be a JSON object",
                    field="selected_filters",
                    expected="object",
                    actual=type(data["selected_filters"]).__name__,
                )
            )

        manifest_field_order = {
            field_name: index
            for index, field_name in enumerate(
                MANIFEST_REQUIRED_FIELDS + ("post_redaction_collisions", "selected_filters")
            )
        }
        issues.sort(key=lambda issue: (manifest_field_order.get(issue.field or "", len(manifest_field_order)), issue.code.value))
        metadata = _freeze_manifest(data)
        declared_record_count = data.get("record_count") if is_nonnegative_integer(data.get("record_count")) else None
        declared_sha256 = data.get("sha256") if isinstance(data.get("sha256"), str) else None
        schema_version = data.get("schema_version") if is_nonnegative_integer(data.get("schema_version")) else None
        format_version = data.get("format_version") if isinstance(data.get("format_version"), int) and not isinstance(data.get("format_version"), bool) else None
        created_at = data.get("created_at") if isinstance(data.get("created_at"), str) else None
        state = ManifestValidationState.INVALID if issues else ManifestValidationState.VALID
        return ManifestValidation(
            state,
            manifest_path,
            metadata,
            declared_record_count,
            declared_sha256,
            schema_version,
            format_version,
            created_at,
            tuple(issues),
            issues_truncated,
        )

    @staticmethod
    def _sha256_file(path: Path) -> str | None:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return None
        return digest.hexdigest()

    def verify_dataset_manifest_pair(self, jsonl_path: str | Path, manifest_path: str | Path) -> DatasetManifestPairValidation:
        dataset_path = Path(jsonl_path)
        manifest_path_value = Path(manifest_path)
        dataset = self.validate_dataset(dataset_path)
        manifest = self.validate_manifest(manifest_path_value)
        issues: list[ValidationIssue] = []
        issues_truncated = False

        def add_issue(issue: ValidationIssue) -> None:
            nonlocal issues_truncated
            if len(issues) < DATASET_VALIDATION_MAX_ISSUES:
                issues.append(issue)
            else:
                issues_truncated = True

        if dataset.state is not DatasetValidationState.VALID:
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.PAIR,
                    ValidationIssueCode.DATASET_INVALID,
                    "Dataset validation did not succeed",
                    expected=DatasetValidationState.VALID.value,
                    actual=dataset.state.value,
                )
            )
        if manifest.state is not ManifestValidationState.VALID:
            add_issue(
                ValidationIssue(
                    ValidationIssueSource.PAIR,
                    ValidationIssueCode.MANIFEST_INVALID,
                    "Manifest validation did not succeed",
                    expected=ManifestValidationState.VALID.value,
                    actual=manifest.state.value,
                )
            )

        actual_record_count = None if dataset.state is DatasetValidationState.UNREADABLE else dataset.nonblank_line_count
        declared_record_count = manifest.declared_record_count
        actual_sha256 = self._sha256_file(dataset_path)
        declared_sha256 = manifest.declared_sha256
        record_count_matches: bool | None = None
        sha256_matches: bool | None = None
        if dataset.state is DatasetValidationState.VALID and manifest.state is ManifestValidationState.VALID:
            if actual_record_count is not None and declared_record_count is not None:
                record_count_matches = actual_record_count == declared_record_count
                if not record_count_matches:
                    add_issue(
                        ValidationIssue(
                            ValidationIssueSource.PAIR,
                            ValidationIssueCode.RECORD_COUNT_MISMATCH,
                            "Dataset record count does not match manifest record count",
                            field="record_count",
                            expected=declared_record_count,
                            actual=actual_record_count,
                        )
                    )
            if actual_sha256 is not None and declared_sha256 is not None:
                sha256_matches = actual_sha256.casefold() == declared_sha256.casefold()
                if not sha256_matches:
                    add_issue(
                        ValidationIssue(
                            ValidationIssueSource.PAIR,
                            ValidationIssueCode.SHA256_MISMATCH,
                            "Dataset SHA-256 does not match manifest SHA-256",
                            field="sha256",
                            expected=declared_sha256,
                            actual=actual_sha256,
                        )
                    )

        if dataset.state is DatasetValidationState.VALID and manifest.state is ManifestValidationState.VALID and not issues:
            state = PairValidationState.VALID
        elif dataset.state is DatasetValidationState.UNREADABLE or manifest.state in {
            ManifestValidationState.MISSING,
            ManifestValidationState.UNREADABLE,
        }:
            state = PairValidationState.UNREADABLE
        else:
            state = PairValidationState.INVALID
        return DatasetManifestPairValidation(
            state,
            dataset_path,
            manifest_path_value,
            dataset,
            manifest,
            actual_record_count,
            declared_record_count,
            record_count_matches,
            actual_sha256,
            declared_sha256,
            sha256_matches,
            None,
            tuple(issues),
            issues_truncated,
        )
