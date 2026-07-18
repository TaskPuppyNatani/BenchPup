# BenchPup Architecture

# Architecture Principles

## Core Engine First

BenchPup supports multiple user interfaces.

The Core Engine is the single source of truth for all business logic.

The CLI, GUI, and any future interfaces (Web, API, scripting, etc.) must reuse
the Core Engine rather than implementing their own logic.

Responsibilities are divided as follows:

### Core Engine

Responsible for:

- Validation
- CRUD operations
- Repository access
- Statistics
- Reports
- Imports
- Exports
- Backup
- Restore
- Dataset generation
- Business rules

The Core Engine must not depend on any specific user interface.

### CLI

Responsible only for:

- Screen rendering
- Keyboard navigation
- Menus
- User prompts
- Progress display

The CLI must never duplicate business logic.

### GUI

Responsible only for:

- Windows
- Dialogs
- Widgets
- Tables
- Charts
- Drag & Drop
- Visualization

The GUI must never duplicate business logic.

### Future Interfaces

Future interfaces such as a Web UI, REST API, or scripting interface should
also call the Core Engine rather than implementing their own logic.

## Design Goal

Every feature should be implemented once in the Core Engine.

The CLI and GUI are two different front ends over the same engine.

Adding a new interface should require little more than a new presentation layer.


## Domain Data Classes

```text
ModelProfile
HardwareProfile
BenchmarkSession
BenchmarkDefinition
PromptTemplate
ReviewScore
BenchmarkRun
RunAttachment
ScoreboardImportBatch
ScoreboardEntry
ExportProfile
```

`BenchmarkRun` owns model, prompt, benchmark, and hardware snapshots.
`ScoreboardEntry` is a first-class historical summary record grouped through
`ScoreboardImportBatch`.

## Engine Boundaries

```text
CLI / future PySide6 GUI
        |
        +-- Application services
        |     Sessions, Profiles, Definitions, Templates, Hardware
        |     Runs, Scores, Attachments, Scoreboard, Export, Archive
        |     Search, Statistics
        |
        +-- Import subsystem
        |     CSV mapping/import
        |     Hardware parser registry
        |       MSInfo32
        |       DXDiag
        |       lshw --short
        |
        +-- Reporting subsystem
        |     CSV, Markdown, Dataset JSONL, standalone HTML
        |
        +-- Archive subsystem
              Versioned JSON export
              Validation and preview
              Transactional merge
              Safe replace
              Pre-restore safety backup

                    |
            Repositories + migrations
                    |
                  SQLite
```

## Import Architecture

CSV import decodes supported encodings, normalizes headings, detects file type,
auto-maps columns, previews records, validates transactionally, handles
duplicates, and commits or rolls back.

Hardware import uses a parser registry. Each parser exposes `source_name`,
`can_parse(text)`, and `parse(text) -> HardwareProfileDraft`.

## Archive Architecture

Export gathers all first-class entities, writes a temporary UTF-8 JSON file,
validates it, and atomically replaces the destination.

Preview performs no writes.

Merge restore remaps IDs in dependency order and rolls back on failure.

Replace restore creates a safety backup, restores into a temporary database,
runs migrations and foreign-key checks, opens it through the normal database
layer, and swaps only after all checks succeed.

## Dataset Builder Architecture

`engine.datasets.DatasetBuilder` is the reusable training-data boundary for
both the CLI and the future GUI. Its public operations are:

- `preview(runs, filters, redaction_config)`
- `build_records(...)`
- `write_dataset(...)`
- `validate_dataset(path)`
- `validate_manifest(path)`
- `verify_dataset_manifest_pair(jsonl_path, manifest_path)`

The engine owns eligibility classification, warning collection, filters,
source-content/fingerprint/near-duplicate accounting, redaction, JSONL v1
transformation, manifest construction, staged output, and validation. It
returns structured preview, validation, and write results rather than printing
or depending on terminal state. Source database objects are never changed by
the builder.

The screen-based CLI owns only session-local `DatasetFilters` and
`RedactionConfig` state, vertical configuration screens, path selection,
confirmation, and presentation of engine results. Preview, build, and existing
dataset validation all call the DatasetBuilder directly; the CLI does not
reimplement eligibility, hashing, JSON parsing, duplicate detection, or file
writing.

## Reporting Engine Foundation

`engine.reporting` is the UI-independent boundary for Phase 4.2A. Its public
`ReportingService` and `build_*` APIs own record selection, aggregation,
structured report results, and portable Markdown rendering for the two separate
source families:

- `BenchmarkRunAggregate` records combine a run with its `ReviewScore`, session,
  and attachment metadata when available.
- `ScoreboardEntryAggregate` records keep historical `ScoreboardEntry` values
  separate and associate an optional `ScoreboardImportBatch`.

`build_benchmark_run_report()` produces detailed model-grouped run reports;
`build_scoreboard_report()` produces batch-grouped historical reports; and
`build_model_leaderboard()` produces deterministic model rankings from detailed
runs. `render_*_markdown()` functions are UI-independent. Prompt text, raw
model output, and attachment metadata are opt-in, and attachment binary
contents are never read by the reporting engine.

Soft-deleted runs are excluded by default. Scoreboard entries whose entry or
source batch is soft-deleted are also excluded by default. Numeric summaries
ignore missing scores and missing tokens-per-second values rather than treating
them as zero. Leaderboard ordering is average overall score descending, then
scored-run count descending, median score descending, and model name ascending
(case-insensitive, then original spelling).

`write_markdown_report()` provides staged UTF-8 output with explicit overwrite
protection and structured `ReportWriteResult` statuses. It never prompts or
owns CLI destination selection.
