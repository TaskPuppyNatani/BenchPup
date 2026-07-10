from __future__ import annotations

import csv
import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

    def preview(self, path: str | Path, mapping: dict[str, str | None] | None = None, *, summary: bool = False) -> ImportPreview:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as source:
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
            row_numbers = []
            skipped_rows = []
            for row_number, source_row in enumerate(reader, start=2):
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
        return ImportPreview(headings, resolved, rows, unknown, row_numbers, skipped_rows)

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

    def import_rows(self, rows: list[dict[str, str]], duplicate_policy: str = "skip") -> ImportResult:
        if duplicate_policy not in {"skip", "replace", "keep"}:
            raise ValueError("duplicate_policy must be skip, replace, or keep")
        # Build and validate every record before any database mutation.
        records = [self._build(row, number) for number, row in enumerate(rows, start=2)]
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
                    score.run_id = cursor.lastrowid
                    score_values = self.benchmarks.scores._values(score)
                    score_columns = self.benchmarks.scores.columns
                    connection.execute(f"INSERT INTO review_scores ({', '.join(score_columns)}) VALUES ({', '.join('?' for _ in score_columns)})", [score_values[column] for column in score_columns])
                existing.add(run.fingerprint); seen.add(run.fingerprint); result.imported += 1
        return result

    def import_scoreboard_entries(self, rows: list[dict[str, str]], source_file: str | Path, batch_name: str | None = None, batch_notes: str = "", row_numbers: list[int] | None = None) -> ImportResult:
        entries = []
        row_numbers = row_numbers or list(range(2, len(rows) + 2))
        for row_number, row in zip(row_numbers, rows):
            try:
                model_name = self._value(row, "model_name")
                if not model_name: raise ValueError("model_name is required")
                entry = ScoreboardEntry(
                    model_name=model_name, temperature=self._value(row, "temperature", None),
                    moe_experts=self._value(row, "moe_experts"), context_length=self._value(row, "context_length", None),
                    tokens_per_second=self._value(row, "tokens_per_second", None),
                    review_quality=self._value(row, "review_quality"), score=self._value(row, "score", None),
                    hallucination_level=self._value(row, "hallucination_level"), consistency=self._value(row, "consistency"),
                    reliability_score=self._value(row, "reliability_score"), verdict=self._value(row, "verdict"),
                    notes=self._value(row, "notes"), notes_extra=self._value(row, "notes_extra"), source_file=str(source_file))
                entry.validate(); entries.append(entry)
            except ValueError as error:
                raise ValueError(f"Row {row_number}: {error}") from None
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
