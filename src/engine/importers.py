from __future__ import annotations

import csv
import codecs
import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .domain import BENCHMARK_TYPES, BenchmarkRun, ReviewScore, ScoreboardEntry, ScoreboardImportBatch, now
from .services import BenchmarkService


IMPORT_FIELDS = {
    "model_name", "model_family", "model_size", "quantization", "backend",
    "temperature", "top_p", "top_k", "min_p", "thinking_enabled",
    "flash_attention", "moe_experts", "context_length", "tokens_per_second",
    "benchmark_file", "benchmark_type", "prompt_name", "prompt_text",
    "raw_model_output", "accuracy_score", "hallucination_level",
    "reliability_level", "depth_score", "signal_noise_score",
    "actionability_score", "seniority_score", "overall_score", "strengths",
    "weaknesses", "verdict", "notes", "created_at",
    # Spreadsheet-only fields are retained in review notes because the engine
    # does not model them as separate score columns yet.
    "review_quality_notes", "reliability_score",
}

MAPPING_FIELDS = (
    "model_name", "model_family", "model_size", "quantization", "backend",
    "temperature", "top_p", "top_k", "context_length", "moe_experts",
    "tokens_per_second", "overall_score", "hallucination_level",
    "reliability_level", "notes",
)

SUMMARY_IMPORT_FIELDS = {
    "model_name", "temperature", "moe_experts", "context_length", "tokens_per_second",
    "review_quality", "score", "hallucination_level", "consistency",
    "reliability_score", "verdict", "notes", "notes_extra",
}
SUMMARY_MAPPING_FIELDS = (
    "model_name", "temperature", "moe_experts", "context_length", "tokens_per_second",
    "review_quality", "score", "hallucination_level", "consistency",
    "reliability_score", "verdict", "notes", "notes_extra",
)

BENCHMARK_RUN_IMPORT = "benchmark_run"
SCOREBOARD_IMPORT = "scoreboard"
AMBIGUOUS_IMPORT = "ambiguous"
UNSUPPORTED_IMPORT = "unsupported"
CSV_IMPORT_TYPES = (BENCHMARK_RUN_IMPORT, SCOREBOARD_IMPORT)
CSV_DETECTION_STATUSES = (
    BENCHMARK_RUN_IMPORT,
    SCOREBOARD_IMPORT,
    AMBIGUOUS_IMPORT,
    UNSUPPORTED_IMPORT,
)
CsvImportType = Literal["benchmark_run", "scoreboard"]
CsvDetectionStatus = Literal["benchmark_run", "scoreboard", "ambiguous", "unsupported"]

ALIASES = {
    "model": "model_name", "model_name": "model_name", "backend": "backend",
    "experts": "moe_experts", "context": "context_length", "tok_s": "tokens_per_second",
    "speed": "tokens_per_second", "tps": "tokens_per_second", "benchmark": "benchmark_file",
    "score": "overall_score", "rating": "overall_score",
    "temp": "temperature", "temperature": "temperature", "top_p": "top_p",
    "top_p_sampling": "top_p", "hallucination": "hallucination_level",
    "hallucinations": "hallucination_level", "hallucination_level": "hallucination_level",
    "reliability": "reliability_level", "consistency": "reliability_level",
    "reliability_level": "reliability_level", "reliability_score": "reliability_score",
    "review_quality": "review_quality_notes", "notes_extra": "notes", "verdict": "verdict",
}

SUMMARY_ALIASES = {
    "model": "model_name", "model_name": "model_name", "temp": "temperature",
    "temperature": "temperature", "experts": "moe_experts", "context": "context_length",
    "tok_s": "tokens_per_second", "speed": "tokens_per_second", "tps": "tokens_per_second",
    "review_quality": "review_quality", "score": "score", "rating": "score",
    "hallucination": "hallucination_level", "hallucinations": "hallucination_level",
    "consistency": "consistency", "reliability_score": "reliability_score",
    "reliability": "reliability_score", "verdict": "verdict", "notes": "notes",
    "notes_extra": "notes_extra",
}

FLOAT_FIELDS = {"temperature", "top_p", "min_p", "tokens_per_second", "accuracy_score",
                "depth_score", "signal_noise_score", "actionability_score", "seniority_score", "overall_score",
                "score"}
INT_FIELDS = {"top_k", "context_length"}
BOOL_FIELDS = {"thinking_enabled", "flash_attention"}
SCORE_FIELDS = {"accuracy_score", "hallucination_level", "reliability_level", "depth_score",
                "signal_noise_score", "actionability_score", "seniority_score", "overall_score",
                "strengths", "weaknesses", "verdict", "notes", "review_quality_notes", "reliability_score"}


def normalize_context_length(value: str) -> int | None:
    """Parse scoreboard context values using decimal SI suffixes (k=1,000; m=1,000,000)."""
    raw = value.strip()
    if raw.lower() in {"", "-", "n/a"}:
        return None
    compact = raw.replace(",", "")
    match = re.fullmatch(r"(\d+)\s*([kKmM])?", compact)
    if not match:
        raise ValueError(f"context_length must be an integer (got {value!r})")
    amount, suffix = match.groups()
    multiplier = {None: 1, "k": 1_000, "m": 1_000_000}[suffix.lower() if suffix else None]
    return int(amount) * multiplier


def normalize_heading(heading: str) -> str:
    """Turn human-oriented Sheet headings into stable import field names."""
    cleaned = re.sub(r"[\s\-/]+", "_", heading.strip().lower())
    cleaned = re.sub(r"[^a-z0-9_]", "", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if cleaned in ALIASES:
        return ALIASES[cleaned]
    if cleaned in IMPORT_FIELDS:
        return cleaned
    candidates = {**{field: field for field in IMPORT_FIELDS}, **ALIASES}
    match = difflib.get_close_matches(cleaned, candidates.keys(), n=1, cutoff=0.68)
    return candidates[match[0]] if match else cleaned


def normalize_summary_heading(heading: str) -> str:
    cleaned = re.sub(r"[\s\-/]+", "_", heading.strip().lower())
    cleaned = re.sub(r"[^a-z0-9_]", "", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if cleaned in SUMMARY_ALIASES: return SUMMARY_ALIASES[cleaned]
    if cleaned in SUMMARY_IMPORT_FIELDS: return cleaned
    candidates = {**{field: field for field in SUMMARY_IMPORT_FIELDS}, **SUMMARY_ALIASES}
    match = difflib.get_close_matches(cleaned, candidates.keys(), n=1, cutoff=0.68)
    return candidates[match[0]] if match else cleaned


@dataclass
class ImportPreview:
    headings: list[str]
    mapping: dict[str, str | None]
    rows: list[dict[str, str]]
    unknown_headings: list[str]
    row_numbers: list[int]
    skipped_rows: list[tuple[int, str]]
    source_rows: list[dict[str, str]] = field(default_factory=list)
    source_row_numbers: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class ImportFieldMetadata:
    """Engine-owned metadata describing one mapping destination."""

    name: str
    required: bool


@dataclass(frozen=True)
class CsvImportDetection:
    """Engine-owned CSV detection information for GUI and other frontends."""

    status: CsvDetectionStatus
    import_type: CsvImportType | None
    encoding: str | None
    preview: ImportPreview | None
    reason: str


@dataclass(frozen=True)
class ImportMappingIssue:
    """One engine-owned mapping error suitable for user-facing display."""

    destination: str | None
    source_headings: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class ImportMappingValidationResult:
    """Non-mutating validation result for one CSV heading mapping."""

    errors: tuple[ImportMappingIssue, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class ImportValidationIssue:
    """One source-row validation error discovered without database writes."""

    row_number: int
    message: str


@dataclass(frozen=True)
class ImportValidationResult:
    """Non-mutating validation and duplicate information for an import preview."""

    errors: tuple[ImportValidationIssue, ...] = ()
    duplicate_count: int = 0

    @property
    def is_valid(self) -> bool:
        return not self.errors


class UnsupportedCsvEncodingError(ValueError):
    """Raised when a CSV uses an encoding unsupported by Phase 5D1A."""

    def __init__(self, encoding: str) -> None:
        self.encoding = encoding
        super().__init__(f"CSV encoding {encoding} is not supported")


class ImportMappingError(ValueError):
    """Raised when an explicit mapping is not safe to preview or commit."""

    def __init__(self, validation: ImportMappingValidationResult) -> None:
        self.validation = validation
        super().__init__("; ".join(issue.message for issue in validation.errors))


@dataclass
class ImportResult:
    imported: int = 0
    skipped: int = 0
    replaced: int = 0
    duplicates: int = 0


class CsvImportService:
    def __init__(self, benchmarks: BenchmarkService):
        self.benchmarks = benchmarks

    @staticmethod
    def detect_encoding(path: str | Path) -> str:
        """Return the supported CSV text encoding indicated by the file bytes."""

        raw = Path(path).read_bytes()
        if raw.startswith(codecs.BOM_UTF32_LE):
            raise UnsupportedCsvEncodingError("utf-32-le")
        if raw.startswith(codecs.BOM_UTF32_BE):
            raise UnsupportedCsvEncodingError("utf-32-be")
        if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
            return "utf-16"
        if raw.startswith(codecs.BOM_UTF8):
            return "utf-8-sig"
        return "utf-8"

    @staticmethod
    def _import_type_from_preview(
        preview: ImportPreview,
    ) -> tuple[CsvDetectionStatus, CsvImportType | None, str]:
        run_mapped = {target for target in preview.mapping.values() if target}
        summary_mapped = {
            normalize_summary_heading(heading)
            for heading in preview.headings
        }
        run_shape = {"benchmark_file", "prompt_text", "raw_model_output"}
        run_evidence = {
            "benchmark_file",
            "benchmark_type",
            "prompt_name",
            "prompt_text",
            "raw_model_output",
        }
        scoreboard_evidence = {
            "review_quality",
            "score",
            "hallucination_level",
            "consistency",
            "reliability_score",
            "verdict",
            "notes",
            "notes_extra",
        }
        if run_shape <= run_mapped:
            return BENCHMARK_RUN_IMPORT, BENCHMARK_RUN_IMPORT, "Benchmark Run fields were detected."
        if run_mapped & run_evidence:
            return AMBIGUOUS_IMPORT, None, "The CSV contains partial Benchmark Run evidence. Choose an import type explicitly."
        if "model_name" in summary_mapped and summary_mapped & scoreboard_evidence:
            return SCOREBOARD_IMPORT, SCOREBOARD_IMPORT, "Scoreboard fields were detected."
        if "model_name" in run_mapped or "model_name" in summary_mapped:
            return AMBIGUOUS_IMPORT, None, "The CSV has recognized model data but no unambiguous import shape. Choose an import type explicitly."
        return UNSUPPORTED_IMPORT, None, "The CSV headings do not match a supported Benchmark Run or Scoreboard import."

    def detect(self, path: str | Path) -> CsvImportDetection:
        """Detect CSV encoding and classify its supported import shape."""

        try:
            encoding = self.detect_encoding(path)
        except UnsupportedCsvEncodingError as error:
            return CsvImportDetection(
                UNSUPPORTED_IMPORT,
                None,
                error.encoding,
                None,
                str(error),
            )
        preview = self.preview(path, encoding=encoding)
        status, import_type, reason = self._import_type_from_preview(preview)
        return CsvImportDetection(status, import_type, encoding, preview, reason)

    @staticmethod
    def mapping_fields(import_type: CsvImportType) -> tuple[str, ...]:
        """Return the engine-supported targets for one CSV import type."""

        if import_type == BENCHMARK_RUN_IMPORT:
            return tuple(sorted(IMPORT_FIELDS))
        if import_type == SCOREBOARD_IMPORT:
            return tuple(sorted(SUMMARY_IMPORT_FIELDS))
        raise ValueError("import_type must be benchmark_run or scoreboard")

    @staticmethod
    def mapping_field_metadata(import_type: CsvImportType) -> tuple[ImportFieldMetadata, ...]:
        """Return required/optional metadata for one CSV import type."""

        required = set(CsvImportService.required_mapping_fields(import_type))
        return tuple(
            ImportFieldMetadata(name, name in required)
            for name in CsvImportService.mapping_fields(import_type)
        )

    @staticmethod
    def required_mapping_fields(import_type: CsvImportType) -> tuple[str, ...]:
        """Return importer-required canonical fields for GUI mapping labels."""

        if import_type in CSV_IMPORT_TYPES:
            return ("model_name",)
        raise ValueError("import_type must be benchmark_run or scoreboard")

    def validate_mapping(
        self,
        mapping: dict[str, str | None],
        import_type: CsvImportType,
    ) -> ImportMappingValidationResult:
        """Validate a complete source-heading mapping without changing persistence."""

        allowed = set(self.mapping_fields(import_type))
        errors: list[ImportMappingIssue] = []
        destinations: dict[str, list[str]] = {}
        for heading, destination in mapping.items():
            if destination is None:
                continue
            if destination not in allowed:
                errors.append(
                    ImportMappingIssue(
                        destination,
                        (heading,),
                        f"Source heading {heading!r} maps to unsupported destination {destination!r}.",
                    )
                )
                continue
            destinations.setdefault(destination, []).append(heading)

        for destination, headings in destinations.items():
            if len(headings) > 1:
                errors.append(
                    ImportMappingIssue(
                        destination,
                        tuple(headings),
                        f"Destination {destination!r} is mapped from multiple source headings: {', '.join(headings)}.",
                    )
                )

        for metadata in self.mapping_field_metadata(import_type):
            if not metadata.required:
                continue
            headings = destinations.get(metadata.name, [])
            if not headings:
                errors.append(
                    ImportMappingIssue(
                        metadata.name,
                        (),
                        f"Required destination {metadata.name!r} must be mapped to exactly one source heading.",
                    )
                )
        return ImportMappingValidationResult(tuple(errors))

    @staticmethod
    def _raise_mapping_error(validation: ImportMappingValidationResult) -> None:
        if not validation.is_valid:
            raise ImportMappingError(validation)

    @staticmethod
    def _scoreboard_skip_reason(source_row: dict[str, str | None], row: dict[str, str]) -> str | None:
        values = [
            str(cell or "").strip()
            for value in source_row.values()
            for cell in (value if isinstance(value, list) else [value])
        ]
        populated = [value for value in values if value]
        if not populated:
            return "blank row"
        if re.match(r"^(legend|notes?|summary|totals?|footer)\b", populated[0], re.IGNORECASE):
            return "metadata row"
        if not row.get("model_name", "").strip() and not any(
            row.get(field, "").strip() for field in ("score", "verdict", "notes", "notes_extra")
        ):
            return "no model or scoreboard data"
        return None

    def preview(
        self,
        path: str | Path,
        mapping: dict[str, str | None] | None = None,
        *,
        summary: bool = False,
        encoding: str | None = None,
    ) -> ImportPreview:
        selected_encoding = encoding or self.detect_encoding(path)
        with Path(path).open("r", encoding=selected_encoding, newline="") as source:
            reader = csv.DictReader(source)
            headings = reader.fieldnames or []
            supplied_mapping = mapping or {}
            fields = SUMMARY_IMPORT_FIELDS if summary else IMPORT_FIELDS
            normalizer = normalize_summary_heading if summary else normalize_heading
            resolved = {}
            for heading in headings:
                target = supplied_mapping.get(heading, normalizer(heading))
                resolved[heading] = target if target in fields else None
            unknown = [heading for heading, target in resolved.items() if target is None]
            rows = []
            source_rows = []
            row_numbers = []
            skipped_rows = []
            source_row_numbers = []
            import_type = SCOREBOARD_IMPORT if summary else BENCHMARK_RUN_IMPORT
            for source_row in reader:
                row_number = reader.line_num
                source_rows.append(
                    {
                        str(heading): "" if value is None else str(value)
                        for heading, value in source_row.items()
                        if heading is not None
                    }
                )
                source_row_numbers.append(row_number)
                row: dict[str, str] = {}
                for heading, value in source_row.items():
                    target = resolved.get(heading)
                    if target in fields:
                        cleaned = (value or "").strip()
                        if summary and target == "context_length":
                            # Preserve invalid text for a useful row-level validation error.
                            try:
                                normalized = normalize_context_length(cleaned)
                                cleaned = "" if normalized is None else str(normalized)
                            except ValueError:
                                pass
                        row[target] = cleaned
                if summary:
                    reason = self._scoreboard_skip_reason(source_row, row)
                    if reason:
                        skipped_rows.append((row_number, reason))
                        continue
                rows.append(row)
                row_numbers.append(row_number)
        if mapping is not None:
            self._raise_mapping_error(self.validate_mapping(resolved, import_type))
        return ImportPreview(
            list(headings),
            resolved,
            rows,
            unknown,
            row_numbers,
            skipped_rows,
            source_rows,
            source_row_numbers,
        )

    def mapping_profiles(self) -> list[tuple[int, str, dict[str, str | None]]]:
        with self.benchmarks.database.connection() as connection:
            rows = connection.execute("SELECT id, name, mapping_json FROM import_mapping_profiles ORDER BY name").fetchall()
        return [(row["id"], row["name"], json.loads(row["mapping_json"])) for row in rows]

    def save_mapping_profile(self, name: str, mapping: dict[str, str | None]) -> None:
        if not name.strip(): raise ValueError("profile name is required")
        allowed_fields = IMPORT_FIELDS | SUMMARY_IMPORT_FIELDS
        saved_mapping = {heading: target for heading, target in mapping.items() if target in allowed_fields or target is None}
        timestamp = now()
        with self.benchmarks.database.connection() as connection:
            connection.execute("INSERT INTO import_mapping_profiles(name, mapping_json, created_at, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET mapping_json = excluded.mapping_json, updated_at = excluded.updated_at", (name.strip(), json.dumps(saved_mapping, sort_keys=True), timestamp, timestamp))

    @staticmethod
    def _value(row: dict[str, str], field: str, default: Any = "") -> Any:
        value = row.get(field, "")
        if value == "":
            return default
        if field in FLOAT_FIELDS:
            try: return float(value)
            except ValueError: raise ValueError(f"{field} must be a number") from None
        if field == "context_length":
            return normalize_context_length(value)
        if field in INT_FIELDS:
            try: return int(value)
            except ValueError: raise ValueError(f"{field} must be an integer") from None
        if field in BOOL_FIELDS:
            if value.lower() in {"true", "1", "yes", "y"}: return True
            if value.lower() in {"false", "0", "no", "n"}: return False
            raise ValueError(f"{field} must be true or false")
        return value

    def _build_scoreboard_entry(
        self,
        row: dict[str, str],
        row_number: int,
        source_file: str | Path,
    ) -> ScoreboardEntry:
        try:
            model_name = self._value(row, "model_name")
            if not model_name:
                raise ValueError("model_name is required")
            entry = ScoreboardEntry(
                model_name=model_name,
                temperature=self._value(row, "temperature", None),
                moe_experts=self._value(row, "moe_experts"),
                context_length=self._value(row, "context_length", None),
                tokens_per_second=self._value(row, "tokens_per_second", None),
                review_quality=self._value(row, "review_quality"),
                score=self._value(row, "score", None),
                hallucination_level=self._value(row, "hallucination_level"),
                consistency=self._value(row, "consistency"),
                reliability_score=self._value(row, "reliability_score"),
                verdict=self._value(row, "verdict"),
                notes=self._value(row, "notes"),
                notes_extra=self._value(row, "notes_extra"),
                source_file=str(source_file),
            )
            entry.validate()
            return entry
        except ValueError as error:
            raise ValueError(f"Row {row_number}: {error}") from None

    def validate_rows(
        self,
        rows: list[dict[str, str]],
        *,
        import_type: CsvImportType,
        row_numbers: list[int] | None = None,
        duplicate_policy: str = "skip",
        source_file: str | Path = "",
    ) -> ImportValidationResult:
        """Validate rows and report duplicates without changing persisted data."""

        numbers = row_numbers or list(range(2, len(rows) + 2))
        if len(numbers) != len(rows):
            raise ValueError("row_numbers must match rows")
        if import_type == BENCHMARK_RUN_IMPORT:
            if duplicate_policy not in {"skip", "replace", "keep"}:
                raise ValueError("duplicate_policy must be skip, replace, or keep")
            records: list[tuple[BenchmarkRun, ReviewScore | None]] = []
            errors: list[ImportValidationIssue] = []
            for row, row_number in zip(rows, numbers):
                try:
                    records.append(self._build(row, row_number))
                except ValueError as error:
                    message = str(error)
                    prefix = f"Row {row_number}: "
                    errors.append(ImportValidationIssue(row_number, message.removeprefix(prefix)))
            duplicate_count = 0
            if not errors:
                with self.benchmarks.database.connection() as connection:
                    existing = {
                        row["fingerprint"]
                        for row in connection.execute("SELECT fingerprint FROM benchmark_runs")
                    }
                seen: set[str] = set()
                for run, _score in records:
                    if run.fingerprint in existing or run.fingerprint in seen:
                        duplicate_count += 1
                    seen.add(run.fingerprint)
            return ImportValidationResult(tuple(errors), duplicate_count)
        if import_type == SCOREBOARD_IMPORT:
            errors = []
            for row, row_number in zip(rows, numbers):
                try:
                    self._build_scoreboard_entry(row, row_number, source_file)
                except ValueError as error:
                    message = str(error)
                    prefix = f"Row {row_number}: "
                    errors.append(ImportValidationIssue(row_number, message.removeprefix(prefix)))
            return ImportValidationResult(tuple(errors), 0)
        raise ValueError("import_type must be benchmark_run or scoreboard")

    def _build(self, row: dict[str, str], row_number: int) -> tuple[BenchmarkRun, ReviewScore | None]:
        try:
            model_name = self._value(row, "model_name")
            if not model_name: raise ValueError("model_name is required")
            benchmark_type = self._value(row, "benchmark_type", "code_review")
            if benchmark_type not in BENCHMARK_TYPES: raise ValueError("invalid benchmark_type")
            model = {field: self._value(row, field, "Other" if field == "backend" else None if field in FLOAT_FIELDS | INT_FIELDS else False if field in BOOL_FIELDS else "")
                     for field in ("model_name", "model_family", "model_size", "quantization", "backend", "temperature", "top_p", "top_k", "min_p", "thinking_enabled", "flash_attention", "moe_experts", "context_length", "tokens_per_second")}
            benchmark = {"benchmark_file": self._value(row, "benchmark_file", "custom"), "benchmark_type": benchmark_type}
            prompt_name, prompt_text = self._value(row, "prompt_name"), self._value(row, "prompt_text")
            run = BenchmarkRun(raw_model_output=self._value(row, "raw_model_output"), prompt_name=prompt_name,
                               prompt_text=prompt_text, model_snapshot=model, benchmark_snapshot=benchmark,
                               prompt_snapshot={"name": prompt_name, "prompt_text": prompt_text},
                               created_at=self._value(row, "created_at") or now())
            run = self.benchmarks._resolved_run(run)
            has_score = any(row.get(field, "") != "" for field in SCORE_FIELDS)
            if not has_score: return run, None
            notes = self._value(row, "notes")
            for label, field in (("Review quality", "review_quality_notes"), ("Reliability score", "reliability_score")):
                value = self._value(row, field)
                if value: notes = f"{notes}\n{label}: {value}".strip()
            score = ReviewScore(run_id=1,
                accuracy_score=self._value(row, "accuracy_score", None),
                hallucination_level=self._value(row, "hallucination_level", "Medium"),
                reliability_level=self._value(row, "reliability_level", "Medium"),
                depth_score=self._value(row, "depth_score", None), signal_noise_score=self._value(row, "signal_noise_score", None),
                actionability_score=self._value(row, "actionability_score", None), seniority_score=self._value(row, "seniority_score", None),
                overall_score=self._value(row, "overall_score", None), strengths=self._value(row, "strengths"),
                weaknesses=self._value(row, "weaknesses"), verdict=self._value(row, "verdict"), notes=notes)
            score.validate()
            return run, score
        except ValueError as error:
            raise ValueError(f"Row {row_number}: {error}") from None

    def import_rows(
        self,
        rows: list[dict[str, str]],
        duplicate_policy: str = "skip",
        row_numbers: list[int] | None = None,
    ) -> ImportResult:
        if duplicate_policy not in {"skip", "replace", "keep"}:
            raise ValueError("duplicate_policy must be skip, replace, or keep")
        # Build and validate every record before any database mutation.
        numbers = row_numbers or list(range(2, len(rows) + 2))
        if len(numbers) != len(rows):
            raise ValueError("row_numbers must match rows")
        records = [self._build(row, number) for row, number in zip(rows, numbers)]
        result = ImportResult()
        with self.benchmarks.database.connection() as connection:
            existing = {row["fingerprint"] for row in connection.execute("SELECT fingerprint FROM benchmark_runs")}
            seen: set[str] = set()
            for run, score in records:
                duplicate = run.fingerprint in existing or run.fingerprint in seen
                if duplicate:
                    result.duplicates += 1
                    if duplicate_policy == "skip":
                        result.skipped += 1; continue
                    if duplicate_policy == "replace":
                        connection.execute("DELETE FROM benchmark_runs WHERE fingerprint = ?", (run.fingerprint,))
                        existing.discard(run.fingerprint)
                        seen.discard(run.fingerprint)
                        result.replaced += 1
                    else:  # The canonical fingerprint still detects this record; storage needs a unique key.
                        run.fingerprint = hashlib.sha256(f"{run.fingerprint}:{result.imported}".encode()).hexdigest()
                values = self.benchmarks.runs._values(run)
                columns = self.benchmarks.runs.columns
                cursor = connection.execute(f"INSERT INTO benchmark_runs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", [values[column] for column in columns])
                if score:
                    run_id = cursor.lastrowid
                    assert run_id is not None, "Benchmark run insert did not return an ID"
                    score.run_id = run_id
                    score_values = self.benchmarks.scores._values(score)
                    score_columns = self.benchmarks.scores.columns
                    connection.execute(f"INSERT INTO review_scores ({', '.join(score_columns)}) VALUES ({', '.join('?' for _ in score_columns)})", [score_values[column] for column in score_columns])
                existing.add(run.fingerprint); seen.add(run.fingerprint); result.imported += 1
        return result

    def import_scoreboard_entries(self, rows: list[dict[str, str]], source_file: str | Path, batch_name: str | None = None, batch_notes: str = "", row_numbers: list[int] | None = None) -> ImportResult:
        entries = []
        row_numbers = row_numbers or list(range(2, len(rows) + 2))
        if len(row_numbers) != len(rows):
            raise ValueError("row_numbers must match rows")
        for row_number, row in zip(row_numbers, rows):
            entries.append(self._build_scoreboard_entry(row, row_number, source_file))
        source_file = str(source_file)
        batch = ScoreboardImportBatch(name=batch_name or f"{Path(source_file).stem} import {now()[:10]}", source_file=source_file, notes=batch_notes)
        batch.validate()
        with self.benchmarks.database.connection() as connection:
            batch_repository = self.benchmarks.catalog.scoreboard_import_batches
            batch_values, batch_columns = batch_repository._values(batch), batch_repository.columns
            cursor = connection.execute(f"INSERT INTO scoreboard_import_batches ({', '.join(batch_columns)}) VALUES ({', '.join('?' for _ in batch_columns)})", [batch_values[column] for column in batch_columns])
            repository = self.benchmarks.catalog.scoreboard_entries
            for entry in entries:
                entry.import_batch_id = cursor.lastrowid
                values, columns = repository._values(entry), repository.columns
                connection.execute(f"INSERT INTO scoreboard_entries ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", [values[column] for column in columns])
        return ImportResult(imported=len(entries))
