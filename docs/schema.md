# Phase 0 database design

SQLite is the authoritative local store. All timestamps use ISO-8601 UTC text;
all boolean values use `INTEGER` (0 or 1). The application maintains a single
row in `schema_version` and applies numbered, idempotent migrations at startup.

## Tables

| Table | Purpose | Key columns |
| --- | --- | --- |
| `schema_version` | Migration state | `version`, `applied_at` |
| `benchmark_sessions` | A testing batch containing related runs | `id`, `title`, `started_at`, `completed_at`, `is_deleted` |
| `model_profiles` | Reusable model/backend settings | `id`, `name`, `model_name`, `backend`, sampling fields, `is_default` |
| `hardware_profiles` | Reusable hardware/software environment | `id`, `name`, `cpu`, `gpu`, `vram_gb`, `ram_gb`, `backend_versions` |
| `benchmark_definitions` | Named benchmark targets/templates | `id`, `name`, `file_path`, `benchmark_type`, `default_prompt` |
| `prompt_templates` | Versioned benchmark prompts | `id`, `name`, `version`, `prompt_text`, `prompt_hash`, `benchmark_type`, `is_active` |
| `benchmark_runs` | Immutable benchmark result record | `id`, session/profile/definition/template/hardware FKs, snapshots, output, `fingerprint`, timestamps, `is_deleted` |
| `review_scores` | Detailed human score set for one run | `id`, `run_id`, accuracy/depth/signal/actionability/seniority/overall, levels, rationale fields |
| `run_attachments` | Metadata for run artifacts | `id`, `run_id`, `attachment_type`, `file_path`, `original_filename` |
| `export_profiles` | Saved export choices | `id`, `name`, `format`, `field_selection`, `filter_json`, `destination` |
| `change_history` | Undo/redo audit log | `id`, `operation`, `entity_type`, `entity_id`, `before_json`, `after_json`, `created_at`, `undone_at` |

`benchmark_runs.session_id`, `.model_profile_id`, `.benchmark_definition_id`,
`.prompt_template_id`, and `.hardware_profile_id` are nullable foreign keys.
A run must remain readable if a related reusable record is later deleted.
`review_scores.run_id` is unique, so a run has zero or one current evaluation.
`run_attachments.run_id` is one-to-many: a run can have zero or more artifacts.

## Benchmark run fields

The run snapshot stores `model_name`, `model_family`, `model_size`,
`quantization`, `backend`, `temperature`, `top_p`, `top_k`, `min_p`,
`thinking_enabled`, `flash_attention`, `moe_experts`, `context_length`, and
`tokens_per_second`; it also stores `benchmark_file`, `benchmark_type`,
`prompt_name`, `prompt_text`, and `raw_model_output` as `TEXT` (unbounded by
the application). Snapshots make historical results reproducible even after a
profile changes.

It additionally stores `hardware_snapshot` and `prompt_snapshot` as JSON text.
`prompt_snapshot` contains the exact prompt, template name, template version,
and prompt hash used for that run. `hardware_snapshot` captures the CPU, GPU,
memory, operating system, and backend versions at run time. These snapshots are
authoritative for historical comparison; foreign keys provide convenient reuse
and navigation only.

## New reusable entities

`benchmark_sessions` contains `id`, `title`, `description`, `started_at`,
`completed_at`, `notes`, `created_at`, `updated_at`, and `is_deleted`. It groups
runs performed in the same testing batch.

`hardware_profiles` contains `id`, `name`, `cpu`, `gpu`, `vram_gb`, `ram_gb`,
`operating_system`, `backend_versions`, `notes`, `created_at`, and `updated_at`.
`backend_versions` is JSON text so multiple backend/version pairs can be saved.

`prompt_templates` contains `id`, `name`, `version`, `prompt_text`,
`prompt_hash`, `benchmark_type`, `notes`, `created_at`, `updated_at`, and
`is_active`. `prompt_hash` is a SHA-256 hex digest of the exact UTF-8 prompt
text. The unique key is `(name, version)`; changing prompt text requires a new
version rather than overwriting an old one.

`run_attachments` contains `id`, `run_id`, `attachment_type`, `file_path`,
`original_filename`, `notes`, and `created_at`. The application stores artifact
metadata and paths, not binary attachment contents. Valid attachment types are
initially `screenshot`, `raw_text`, `log`, and `other`.

## Constraints and indexes

- `benchmark_type`: `code_review`, `code_generation`, `revision`, or
  `review_the_review`.
- Numeric scores: nullable or 0–5; levels: Low, Low-Medium, Medium,
  Medium-High, High.
- `fingerprint` has a unique index and is calculated from normalized model,
  benchmark, prompt hash, output, hardware snapshot, and settings fields for
  import duplicate checks.
- Index runs by `created_at`, `model_name`, `benchmark_file`, `overall_score`,
  `session_id`, and `is_deleted` for the list/search/statistics views; index
  attachments by `run_id` and templates by `(name, version)`.
