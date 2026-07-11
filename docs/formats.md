# BenchPup Import, Export, Archive, and Training Formats

## Data Families

BenchPup stores two primary data shapes:

1. `BenchmarkRun` for detailed per-test records
2. `ScoreboardEntry` for historical model-summary records grouped by
   `ScoreboardImportBatch`

They remain separate first-class record types.

## Encoding

- CSV imports: UTF-8 and UTF-8 BOM
- Hardware text imports: UTF-8, UTF-8 BOM, UTF-16LE BOM, UTF-16BE BOM
- Exports: UTF-8

## Imports

Supported imports:

- Benchmark Runs CSV
- Scoreboard CSV
- CSV auto-detection
- MSInfo32
- DXDiag
- `lshw --short`
- manual hardware entry

CSV imports support normalization, mapping, preview, duplicate handling, and
transactional validation.

## Standard Exports

- Benchmark Runs CSV
- Scoreboard CSV with batch metadata
- Markdown reports
- JSONL training data from detailed benchmark runs
- standalone interactive Scoreboard HTML

## BenchPup Archive

Backup and restore use a dedicated versioned format:

```json
{
  "format": "benchpup_archive",
  "archive_version": 1,
  "created_at": "2026-07-10T00:00:00Z",
    "benchpup_version": "0.4.1-Alpha",
  "schema_version": 5,
  "counts": {},
  "data": {
    "benchmark_sessions": [],
    "model_profiles": [],
    "hardware_profiles": [],
    "benchmark_definitions": [],
    "prompt_templates": [],
    "benchmark_runs": [],
    "review_scores": [],
    "run_attachments": [],
    "scoreboard_import_batches": [],
    "scoreboard_entries": [],
    "export_profiles": []
  }
}
```

Archive guarantees:

- UTF-8 JSON
- metadata and per-entity counts
- attachment metadata and paths only
- no binary attachment contents
- atomic export through a validated temporary file
- preview with no writes
- transactional merge
- safe replace through a validated temporary database
- automatic pre-restore safety backup
- relationship ID remapping
- exact-duplicate skipping
- friendly rejection of malformed or unsupported archives

## JSONL Dataset Builder

The Dataset Builder produces curated training JSONL v1 from detailed
`BenchmarkRun` records only. `ScoreboardEntry`, scoreboard import batches,
attachments, and binary data never enter this pipeline.

### JSONL v1 Contract

Each nonblank UTF-8 line is exactly one JSON object with these top-level keys:

```json
{
  "instruction": "Evaluate the following benchmark result.",
  "input": {
    "model": {},
    "benchmark": {},
    "prompt_template": {},
    "prompt_text": "",
    "raw_model_output": ""
  },
  "response": {
    "accuracy": null,
    "hallucination": "",
    "reliability": "",
    "depth": null,
    "signal_to_noise": null,
    "actionability": null,
    "seniority": null,
    "overall": null,
    "strengths": "",
    "weaknesses": "",
    "verdict": "",
    "notes": ""
  },
  "metadata": {
    "backend": "",
    "sampling": {},
    "tokens_per_second": null,
    "hardware": {},
    "recorded_at": "",
    "benchpup_version": "0.4.1-Alpha",
    "schema_version": 5,
    "format_version": 1,
    "source_run_id": 123
  }
}
```

`source_run_id` is optional local provenance. It is included by default and
can be omitted for shareable datasets. Run snapshots are authoritative for the
model, benchmark, prompt, and hardware context.

### Eligibility, Warnings, and Filters

The engine excludes records with stable reason codes:

- `soft_deleted`
- `missing_output`
- `missing_review`
- `invalid_review`
- `missing_model_context`
- `missing_benchmark_context`
- `missing_prompt_context`
- `filtered_out`
- `not_benchmark_run` (a defensive rejection of non-run objects)

Warning-only codes are `missing_hardware`, `missing_session`,
`missing_backend`, `missing_sampling`, and `missing_optional_scores`.
Warnings do not mutate or exclude otherwise eligible source records.

Session-local CLI filters support minimum overall score, maximum hallucination,
minimum reliability, verdict, benchmark type, model, session, date range,
prompt template, hardware profile, include/exclude run IDs, and duplicate
policy. `DatasetFilters` also supports optional provenance for API callers.
Source objects remain unchanged throughout preview, redaction, validation, and
export.

### Duplicates and Redaction

The builder calculates source-content duplicate keys before redaction. Exact
source-content duplicates are skipped by default, with deterministic first-run
retention by run ID; they can be retained explicitly. Fingerprint duplicates
and near duplicates are counted for review but retained. Post-redaction
collisions are warning-only because distinct source records can safely become
identical after redaction.

Redaction can apply literal terms, paths, usernames, email addresses,
hostnames/IP addresses, and validated custom regular expressions. The preview
reports total and per-rule redaction counts. Redaction transforms only export
records, never persisted BenchmarkRun, ReviewScore, or snapshot data.

### Validation, Manifest, and Staged Output

`DatasetBuilder.validate_dataset()` validates every nonblank JSONL line and
reports line-numbered JSON or shape errors. Blank lines are allowed.
`validate_manifest()` validates the companion manifest, and
`verify_dataset_manifest_pair()` verifies record count and JSONL SHA-256.
Missing manifests are reported separately from invalid manifests.

The companion `<dataset>.jsonl.manifest.json` records the dataset filename,
record and exclusion counts, source duplicate count, post-redaction collision
count, redaction count, selected filters, BenchPup/schema versions, creation
timestamp, format version, and JSONL SHA-256.

Output uses safe staged replacement: both temporary files are written and
validated before finalization. JSONL and manifest replacement cannot be one
filesystem transaction, so failures are returned as structured statuses:
`success`, `overwrite_required`, `validation_failed`, `temp_write_failed`,
`temp_cleanup_failed`, `jsonl_finalize_failed`, or
`partial_finalization`. Existing output is never silently overwritten.
