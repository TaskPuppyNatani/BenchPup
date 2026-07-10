# Phase 0 import, export, and training-format specification

## Canonical record

All transports originate from the aggregate `BenchmarkRun` object. Field names
are `snake_case`; UTF-8 is required; empty unknown values are `null` in JSON
and blank in CSV. Text fields, including `raw_model_output`, are retained without
truncation. Exports include associated session information, hardware metadata,
prompt template name/version/hash, and attachment metadata when available.

## Import CSV

The importer reads a Google Sheets CSV export in UTF-8 (including BOM). It
normalizes headings (case, whitespace, and selected aliases such as `Model Name`
to `model_name`) then presents a mapping screen for unmapped headings. The user
previews parsed rows and chooses skip/replace/keep for duplicate fingerprints.
Import is transactional: a rejected validation row does not partially write.

## Exports

- **CSV:** one flattened row per run with session, hardware, and prompt-template
  columns; attachment metadata is represented as a JSON column.
- **JSON:** an array of canonical records, each with nested `session`,
  `hardware`, `prompt_template`, and `attachments`; suitable for backup/integration.
- **Markdown:** a report with run details and selected averages.
- **JSONL:** one training record per line; suitable for dataset pipelines.
- **Leaderboard:** Markdown/CSV aggregation by model, with run count, average,
  median, and score distribution.

## Training JSONL contract

One UTF-8 JSON object per line:

```json
{
  "instruction": "Review this model output and evaluate its quality.",
  "input": {
    "model": "Qwen 3.6 35B A3B",
    "benchmark_file": "speech_server.py",
    "benchmark_type": "code_review",
    "prompt_template": {"name": "standard_review", "version": "2.1", "hash": "sha256:..."},
    "prompt": "...",
    "raw_model_output": "..."
  },
  "response": {
    "accuracy": 5,
    "hallucination": "Low",
    "reliability": "High",
    "overall": 4.8,
    "strengths": "...",
    "weaknesses": "...",
    "verdict": "Excellent coding model...",
    "notes": "..."
  },
  "metadata": {
    "backend": "LM Studio",
    "temperature": 0.3,
    "tokens_per_second": 181,
    "hardware": {
      "name": "Primary workstation",
      "cpu": "...",
      "gpu": "...",
      "vram_gb": 24,
      "ram_gb": 64,
      "operating_system": "Windows 11",
      "backend_versions": {"LM Studio": "..."}
    },
    "session": {"id": 12, "title": "July coding benchmark batch"},
    "recorded_at": "2026-07-09T00:00:00Z"
  }
}
```

The dataset builder must filter soft-deleted runs, permit score/verdict-based
quality thresholds, redact configured sensitive text, and emit a manifest with
record count, selected filters, schema version, and creation timestamp.
Attachments are excluded from training JSONL by default; when included in other
exports they contain only metadata (`attachment_type`, `file_path`,
`original_filename`, `notes`, `created_at`), never binary file contents.
