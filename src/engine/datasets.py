"""Curated JSONL dataset generation from detailed BenchmarkRun records only."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

from .domain import LEVELS, BenchmarkRun, now
from .services import BenchmarkService

DATASET_FORMAT_VERSION = 1


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


@dataclass(frozen=True)
class DatasetValidation:
    state: str
    record_count: int = 0
    message: str = ""
    manifest: dict[str, Any] | None = None


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
    def validate_jsonl(path: str | Path) -> int:
        count = 0
        with Path(path).open("r", encoding="utf-8") as source:
            for number, line in enumerate(source, start=1):
                if not line.strip(): continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"Line {number} contains invalid JSON: {error.msg}") from error
                if not isinstance(record, dict) or set(record) != {"instruction", "input", "response", "metadata"}:
                    raise ValueError(f"Line {number} does not match the JSONL v1 contract")
                count += 1
        return count

    def validate_dataset(self, path: str | Path) -> DatasetValidation:
        try: return DatasetValidation("success", self.validate_jsonl(path))
        except (OSError, ValueError, json.JSONDecodeError) as error: return DatasetValidation("validation_failed", message=str(error))

    @staticmethod
    def validate_manifest(path: str | Path) -> DatasetValidation:
        required = {"dataset_filename", "record_count", "excluded_count", "duplicate_count", "redaction_count", "benchpup_version", "schema_version", "created_at", "format_version", "sha256"}
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not required <= set(data): raise ValueError("Manifest is missing required fields")
            return DatasetValidation("success", manifest=data)
        except FileNotFoundError: return DatasetValidation("manifest_missing", message="Companion manifest was not found")
        except (OSError, ValueError, json.JSONDecodeError) as error: return DatasetValidation("manifest_invalid", message=str(error))

    def verify_dataset_manifest_pair(self, jsonl_path: str | Path, manifest_path: str | Path) -> DatasetValidation:
        dataset = self.validate_dataset(jsonl_path)
        if dataset.state != "success": return dataset
        manifest = self.validate_manifest(manifest_path)
        if manifest.state != "success": return manifest
        assert manifest.manifest is not None
        digest = hashlib.sha256(Path(jsonl_path).read_bytes()).hexdigest()
        if manifest.manifest["sha256"] != digest: return DatasetValidation("manifest_invalid", dataset.record_count, "Manifest SHA-256 does not match dataset", manifest.manifest)
        if manifest.manifest["record_count"] != dataset.record_count: return DatasetValidation("manifest_invalid", dataset.record_count, "Manifest record count does not match dataset", manifest.manifest)
        return DatasetValidation("success", dataset.record_count, manifest=manifest.manifest)
