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

`engine.reporting` is the UI-independent boundary for Phase 4.2. Its public
`ReportingService` and `build_*` APIs own record selection, aggregation,
structured report results, template application, and portable Markdown
rendering for the two separate source families:

- `BenchmarkRunAggregate` records combine a run with its `ReviewScore`, session,
  and attachment metadata when available.
- `ScoreboardEntryAggregate` records keep historical `ScoreboardEntry` values
  separate and associate an optional `ScoreboardImportBatch`.

`build_benchmark_run_report()` produces detailed model-grouped run reports;
`build_scoreboard_report()` produces batch-grouped historical reports;
`build_model_leaderboard()` produces deterministic model rankings from detailed
runs; `build_session_report()` produces one-session summaries; and
`build_hardware_report()` groups runs by normalized historical hardware
snapshots. `render_*_markdown()` functions are UI-independent. Prompt text,
raw model output, and attachment metadata are opt-in, and attachment binary
contents are never read by the reporting engine.

Soft-deleted runs are excluded by default. Scoreboard entries whose entry or
source batch is soft-deleted are also excluded by default. Numeric summaries
ignore missing scores and missing tokens-per-second values rather than treating
them as zero. Leaderboard ordering is average overall score descending, then
scored-run count descending, median score descending, and model name ascending
(case-insensitive, then original spelling).

`ReportTemplate` definitions are immutable built-ins (`Concise`, `Standard`,
and `Full Audit`). `apply_report_template()` returns an isolated mutable
`ReportTemplateOptions` object, so report-specific option edits cannot mutate a
template definition. Templates establish presentation and inclusion defaults;
filters remain independent. Sensitive prompt, raw-output, and attachment
metadata fields remain excluded until explicitly enabled, including for Full
Audit.

`write_markdown_report()` provides staged UTF-8 output with explicit overwrite
protection and structured `ReportWriteResult` statuses. It never prompts or
owns CLI destination selection. The existing `ExportProfile` persistence
model is not used for these built-in templates yet; persisted custom templates
remain future work.

## Statistics Engine Foundation

`engine.statistics` is the UI-independent descriptive-statistics boundary for
Phase 4.4A. `StatisticsService` exposes separate APIs for
`BenchmarkRunAggregate` values and `ScoreboardEntryAggregate` values:
`benchmark_run_statistics()` / `scoreboard_statistics()`, grouped summaries,
and reusable day, week, and month time buckets. The service reuses
`ReportingService` selection and soft-delete rules, then owns the typed
statistical results; callers do not recalculate metrics.

The public immutable models include `NumericSummary`,
`CategoricalDistribution`, source-specific summary and group models, and
`TimeBucketResult`. Numeric summaries retain useful precision and report
total, available, and missing values, mean, median, minimum, maximum,
population standard deviation, optional sample standard deviation, and
quartiles. Missing numeric values are omitted from calculations rather than
converted to zero. Categorical percentages use observed non-missing values as
their denominator, and category maps are safely isolated from source data.

Quartiles use Tukey's median-of-halves method: an odd-population median is
excluded from both halves, while an even population is split into equal
halves. The interquartile range is Q3 minus Q1. Population standard deviation
divides by the full observed count; sample standard deviation is unavailable
below two observed values.

Benchmark-run filters operate on historical snapshots for model, benchmark,
benchmark type, session, hardware, score, review levels, and created-time
ranges. Hardware grouping and normalized-hardware filtering use the same
snapshot normalization as `engine.reporting`; linked catalog records provide
context but cannot rewrite or invalidate a valid historical snapshot. A
missing group value is retained under a stable `Unknown ...` group. Group
results are alphabetically ordered and are not ranked by a metric in this
slice. Scoreboard statistics remain a separate API and preserve import-batch
and soft-delete semantics without converting entries into runs.

Time buckets normalize aware and naive project timestamps to UTC. Days begin
at UTC midnight, weeks begin on Monday at UTC midnight and use ISO week labels,
and months begin on the first UTC day. Invalid or missing timestamps are
excluded from buckets and counted in `TimeBucketResult`; this foundation does
not interpret trends, calculate forecasts, or render charts.

## Reporting CLI Integration

The screen-based CLI exposes the reporting engine through `Data > Reports`.
The Reports screen keeps detailed-run, historical-scoreboard, leaderboard,
session, and hardware options in memory for the current CLI session only. It provides vertical
catalog selectors for benchmark types, benchmarks, sessions, hardware
profiles, models, and scoreboard import batches, plus snapshot-text filters
where a catalog record is not required.

Session reports select one catalog session without requiring the user to type
an ID. Hardware reports can cover all eligible runs or use the shared
benchmark/session/hardware filters. Hardware grouping uses the historical
`BenchmarkRun.hardware_snapshot`; a linked `HardwareProfile` may provide
display context but never rewrites a distinct historical snapshot. Missing
hardware is represented as an explicit `Unknown hardware` group.

Each workflow offers the same friendly template selector. The CLI passes the
engine-produced options to `ReportingService`, preserves filters separately,
and only owns preview, destination autocomplete, confirmation, and structured
write-result presentation.

`TerminalApp` owns only screen navigation, option editing, selection previews,
destination autocomplete, confirmation, and presentation of structured
results. `ReportingService` owns selection, aggregation, ranking, Markdown
rendering, staged UTF-8 writing, and overwrite statuses. Prompt text, raw model
output, and attachment metadata are excluded by default and are passed to the
engine only when explicitly enabled. Attachment binary contents are never
read. An existing report requires a second explicit overwrite confirmation;
cancellation does not write a file. The existing legacy Export screen remains
available separately.
