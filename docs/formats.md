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
- standalone offline HTML analytics dashboards

### Markdown report families

The screen-based Reports workflow writes five Markdown report families through
the reporting engine:

- detailed `BenchmarkRun` reports, grouped by model
- historical `ScoreboardEntry` reports, grouped by `ScoreboardImportBatch`
- deterministic model leaderboards derived from detailed benchmark runs
- one-session reports with session identity, score summaries, distributions,
  and deterministic run sections
- hardware reports grouped by normalized historical `BenchmarkRun` hardware
  snapshots

Report options are session-local. Benchmark reports can filter by benchmark
type, benchmark, session, model, hardware profile, or hardware snapshot text.
Scoreboard reports can filter by import batch and model text. Prompt text, raw
model output, attachment metadata, and leaderboard detail sections are opt-in;
attachment binary contents are never exported. Missing scores and speeds remain
unavailable rather than being filled with zero.

Session reports require a catalog-selected session and exclude soft-deleted
sessions and runs by default. They include represented models, benchmarks, and
hardware environments, score distributions, hallucination and reliability
distributions, and average tokens per second where available. Hardware reports
use normalized snapshot fields (profile name when captured, CPU, GPU, VRAM,
RAM, operating system, backend versions, and other captured details) as the
grouping key. Distinct snapshots remain distinct even when they reference the
same linked profile; missing metadata is shown as `Unknown hardware` rather
than discarded. Hardware group statistics give every eligible run equal
weight.

### Built-in report templates

The engine exposes immutable `ReportTemplate` definitions and
`ReportTemplateOptions` for `Concise`, `Standard`, and `Full Audit`.

- Concise uses summary metadata and compact tables.
- Standard includes normal tables and report details while keeping prompt
  text, raw output, and attachment metadata excluded.
- Full Audit enables the richer hardware/detail presentation, but the same
  privacy-sensitive fields remain excluded until the user explicitly enables
  them.

Applying a template returns an isolated options object. Users may adjust those
options before generation; report filters remain separate and are preserved
when templates change. Internal template identifiers are not entered in the
CLI. Built-in templates are not persisted through `ExportProfile` in this
phase.

Reports use UTF-8 staged output. The CLI previews the structured selection or
ranking result, asks for the destination using the existing path autocomplete,
requires confirmation before writing, and requires a second confirmation to
replace an existing file. Write failures are returned as structured statuses;
the CLI presents them without exposing a traceback for expected errors.

### Descriptive statistics contract

`StatisticsService` exposes separate descriptive summaries for detailed
`BenchmarkRun` aggregates and historical `ScoreboardEntry` aggregates. A
numeric summary reports total records, available and missing values, mean,
median, minimum, maximum, population standard deviation, optional sample
standard deviation, Q1, Q3, and IQR. Missing numbers are never treated as
zero. Engine values are not silently rounded.

Quartiles use Tukey's median-of-halves method: for an odd population the
median is excluded from the lower and upper halves; for an even population the
halves are equal. Population standard deviation divides by `n`; sample
standard deviation divides by `n - 1` and is unavailable for fewer than two
observations. Categorical distributions count missing values separately and
calculate category percentages from observed non-missing values. Category
ordering is deterministic.

Benchmark-run groups support model, benchmark, benchmark type, session, and
historical hardware environment. Scoreboard groups support model and import
batch. Every group has a stable identity, human-readable label, record count,
scored count, and the applicable numeric and categorical summaries. Missing
metadata is retained as an explicit `Unknown ...` group. Group order is
alphabetical; the engine does not rank groups in this phase. Hardware keys are
derived from the authoritative `BenchmarkRun.hardware_snapshot`, so a later
catalog edit does not change historical statistics.

The reusable time-bucket result supports day, week, and month. Timestamps are
normalized to UTC; daily and monthly buckets start at UTC midnight, and weekly
buckets start Monday at UTC midnight with ISO week labels. Each bucket reports
its start, label, record count, scored count, score summary, and speed summary.
Missing or invalid timestamps are excluded and counted separately. These
statistics are consumed by `TrendService`, which adds trend grouping and
neutral first-to-last deltas without forecasting, charts, or GUI presentation.

### Model and session comparison reports

Phase 4.4B comparisons use detailed `BenchmarkRun` snapshots only. The
`ComparisonService` accepts two or more snapshot model names or
`BenchmarkSession` selections and returns immutable structured results with
comparison metadata, selected entities, contributing counts, active filters,
broad summaries, categorical counts/percentages/missing values, aligned
summaries, pairwise deltas when exactly two entities are selected, and
deterministic rankings for models.

Model alignment uses the intersection of benchmark snapshot identities and
reports each selected model's non-overlapping benchmarks separately. Session
alignment reports shared models, shared benchmarks, shared model/benchmark
pairs, and the corresponding non-overlap. Broad and aligned tables retain
record/scored counts and score/speed summaries, so unequal coverage is visible
and is not silently treated as an apples-to-apples result.

The pairwise contract reports mean score, median score, mean tokens per second,
scored count, low-hallucination percentage, and high-reliability percentage.
Every delta is second-minus-first. Percentage deltas are marked unavailable
when either value is missing or the first value is zero. Missing numeric values
are omitted rather than converted to zero; categorical percentages use
observed values as the denominator and keep missing counts separate. Deleted
runs and, for session selection, deleted sessions are excluded by default.

`Model Comparison` and `Session Comparison` are screen-based CLI workflows
under `Data > Comparisons`. The CLI uses vertical multi-selectors, optional
benchmark/type/session/hardware/date/score/review filters, structured previews,
and the existing path autocomplete, confirmation, overwrite confirmation, and
staged UTF-8 Markdown writer. It does not calculate metrics, rank entities, or
accept manually typed internal IDs. Cancellation does not write output.

Comparison Markdown contains title/generated/selection/filter metadata, broad
and aligned summaries, overlap/non-overlap, categorical distributions,
pairwise results, rankings where applicable, and a methodology note. It does
not export raw model output, prompt text, attachments, charts, trends,
forecasts, significance claims, or confidence claims.

### Trend reports

Trend reports are Markdown renderings of an immutable `TrendReport` returned
by `TrendService`. `BenchmarkRun` and `ScoreboardEntry` are separate source
families and are never converted into one another.

Each trend report carries:

- title, generated timestamp, source family, interval, grouping, and selected
  date range
- active filters, contributing timestamp-valid record count, and excluded
  missing/invalid timestamp count
- an overall aggregate `TrendSeries` and deterministic grouped series
- ordered `TrendPoint` values with bucket label, record/scored/missing-score
  counts, `NumericSummary` score and speed values, categorical distributions,
  and represented snapshot labels
- first/last available summaries, last-minus-first absolute deltas, and
  percentage deltas only when the baseline is nonzero and available
- per-series coverage values, composition notes, excluded-data notes, and a
  methodology note

Timeline values are normalized to UTC. Day, week, and month boundaries match
the statistics contract: UTC midnight days, Monday-start ISO weeks, and
first-of-month UTC buckets. Missing or invalid timestamps are excluded and
counted. Missing scores and speed values are omitted from calculations rather
than zero-filled. Empty buckets are omitted by default; the explicit
`include_empty_buckets` option creates only intervening empty buckets with
zero counts and unavailable numeric values. Empty buckets do not affect
deltas and are not interpolated.

Trend deltas are descriptive last-minus-first differences. The format does not
call a positive delta an improvement or a negative delta a regression, and it
does not claim causation, statistical significance, confidence, smoothing, or
forecasting. Coverage warnings such as different date ranges, changing model
or benchmark composition, hardware composition changes, sparse series, and a
single populated bucket are descriptive context rather than quality verdicts.

BenchmarkRun grouping uses authoritative model and benchmark snapshots,
benchmark type, session metadata, and normalized historical hardware
snapshots. Scoreboard grouping uses model and import-batch metadata with
`imported_at` as its timeline. Deleted-source and deleted-batch behavior
follows the existing selection contract; missing grouping metadata is retained
under stable `Unknown` labels.

The Markdown renderer includes metadata, filters, coverage notes, series
summary tables, chronological bucket tables, concise distributions, and
unavailable-value markers. It excludes raw model output, prompt text,
attachments, and HTML-dependent markup. Output uses the existing staged UTF-8
writer and explicit overwrite statuses. The screen CLI owns navigation,
vertical selectors, preview, destination selection, and confirmation only;
trend calculations remain in `TrendService`.

### Standalone HTML analytics report

The standalone analytics report is a single self-contained UTF-8 HTML file
generated by `engine.html_reporting`. `BenchmarkRun` and `ScoreboardEntry`
remain separate source families. A combined selection renders two independent
dashboards rather than merging incompatible records or weighting one family by
the other.

The typed report models prepare all values before rendering. BenchmarkRun
dashboards cover model quality and speed, score/hallucination/reliability
distributions, score and speed trends, benchmark summaries, and optional
historical hardware summaries. ScoreboardEntry dashboards cover score and
speed by model, score/hallucination/consistency/reliability distributions,
imported-time score trends, and import-batch summaries. Dashboard metadata
shows contributing/scored counts, represented models, benchmarks, sessions,
hardware environments or import batches, date range, means/medians, active
filters, and coverage warnings.

Numeric and categorical values reuse `StatisticsService`; trend values reuse
`TrendService`; any comparison summaries are already structured engine
results. Missing values remain unavailable and are counted separately, and
empty charts are omitted with a clear reason. The browser performs no metric
calculation, interpolation, forecasting, significance testing, or network
access.

The file has inline CSS, inline SVG, accessible chart data tables, detailed
tables containing only safe analytics fields, and a `noscript` fallback. The
renderer excludes prompts, raw model output, and attachment contents. It HTML-
escapes visible content and applies explicit script-safe JSON serialization
before embedding the typed dataset. No external scripts, stylesheets, images,
CDNs, APIs, or fonts are required. Small presentation controls can show or
hide chart series and search/sort the rendered detail table; the static tables
remain available when JavaScript is disabled.

The existing Export screen keeps its legacy options and adds `6) HTML
Analytics Report`. Configuration is session-local and includes source family,
sections, trend interval, filters, preview, destination autocomplete, write
confirmation, and overwrite confirmation. The writer stages a same-directory
temporary file and returns the existing structured write statuses; the default
CLI destination is `benchpup-analytics.html`.

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
