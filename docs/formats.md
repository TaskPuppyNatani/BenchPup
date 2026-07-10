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
  "benchpup_version": "0.4.0-alpha",
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

## Training JSONL

Training JSONL remains sourced from detailed benchmark runs and can include
prompt metadata, hardware metadata, session metadata, review scores, and a
manifest. The future dataset builder should support filters, redaction,
deduplication, validation, and preview.
