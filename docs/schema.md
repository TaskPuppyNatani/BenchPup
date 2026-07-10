# BenchPup Database Design

SQLite is the authoritative local store. All timestamps use ISO-8601 UTC text.
Boolean values use `INTEGER` (`0` or `1`). Numbered, idempotent migrations are
applied at startup.

## Current Migration State

The current application includes migrations through the hardware-profile import
changes that added `computer_name`, `import_source`, and `imported_at`.

## Tables

| Table | Purpose | Key columns |
| --- | --- | --- |
| `schema_version` | Migration state | `version`, `applied_at` |
| `benchmark_sessions` | Related benchmark-run batches | `id`, `title`, `started_at`, `completed_at`, `is_deleted` |
| `model_profiles` | Reusable model/backend settings | `id`, `name`, `model_name`, `backend`, sampling fields, `is_default` |
| `hardware_profiles` | Reusable hardware/software environments | `id`, `name`, `computer_name`, `cpu`, `gpu`, `vram_gb`, `ram_gb`, `operating_system`, `backend_versions`, `import_source`, `imported_at` |
| `benchmark_definitions` | Named benchmark targets/templates | `id`, `name`, `file_path`, `benchmark_type`, `default_prompt` |
| `prompt_templates` | Versioned benchmark prompts | `id`, `name`, `version`, `prompt_text`, `prompt_hash`, `benchmark_type`, `is_active` |
| `benchmark_runs` | Detailed historical benchmark records | `id`, related FKs, snapshots, output, `fingerprint`, timestamps, `is_deleted` |
| `review_scores` | Current detailed evaluation for a run | `id`, `run_id`, score fields, levels, rationale fields |
| `run_attachments` | Metadata for run artifacts | `id`, `run_id`, `attachment_type`, `file_path`, `original_filename` |
| `scoreboard_import_batches` | Provenance for imported historical scoreboards | `id`, `name`, `source_file`, `imported_at`, `notes`, `is_deleted` |
| `scoreboard_entries` | Historical model-summary rows | `id`, `import_batch_id`, model/settings/score fields, verdict, notes, timestamps, `is_deleted` |
| `export_profiles` | Saved export choices | `id`, `name`, `format`, `field_selection`, `filter_json`, `destination` |
| `change_history` | Undo/redo audit data | `id`, `operation`, `entity_type`, `entity_id`, `before_json`, `after_json`, `created_at`, `undone_at` |

## Relationships

Benchmark runs use nullable foreign keys for session, model profile, benchmark
definition, prompt template, and hardware profile. Runs remain readable because
they preserve snapshots.

`review_scores.run_id` is unique. `run_attachments.run_id` is one-to-many.
`scoreboard_entries.import_batch_id` links entries to their source batch.

## Hardware Profiles

Hardware profiles include:

- name
- computer name
- CPU
- GPU
- VRAM
- RAM
- operating system
- backend versions
- notes
- import source
- imported timestamp

`import_source` records provenance such as `MSInfo32`, `DXDiag`,
`lshw --short`, or manual entry.

## Scoreboard Entries

Scoreboard entries are historical summary records and remain separate from
detailed benchmark runs. They do not require benchmark, prompt, or raw-output
fields.

Context values accept integers, optional commas, decimal `k`/`m` suffixes, and
blank/`-`/`N/A` as null.

## Backup and Restore

Backup/restore adds no new database tables. The archive layer serializes all
first-class entities, validates them, remaps relationships during restore, and
uses the existing migration system.
