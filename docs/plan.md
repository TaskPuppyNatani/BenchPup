# BenchPup Roadmap

## Phase 0 — Architecture

Define the database schema, domain models, import/export contracts, CLI flow,
GUI wireframe, and training dataset contract.

**Status: Complete**

## Phase 1 — Core Engine

Build SQLite migrations/versioning and UI-independent CRUD services for all
first-class entities, with validation, search foundations, and historical
snapshots.

**Status: Complete**

## Phase 2 — CLI

Add the complete interactive CLI with catalog management, benchmark workflows,
validation, navigation, remembered defaults, path autocomplete, import/export,
and backup/restore flows.

**Status: Complete**

## Phase 3 — Import

Completed:

- Benchmark Runs CSV import
- Scoreboard CSV import
- CSV type auto-detection
- heading normalization and mapping
- preview and transactional validation
- duplicate handling
- scoreboard import batches
- MSInfo32, DXDiag, and `lshw --short` hardware import
- UTF-8, UTF-8 BOM, and UTF-16 BOM handling

**Status: Complete**

## Phase 3.5 — Reporting Preview and CLI Power Features

Completed:

- interactive standalone Scoreboard HTML report
- search, filters, sorting, expandable rows, and summary cards
- batch-aware grouping
- Windows path autocomplete
- improved export destination handling

**Status: Complete**

## Phase 4 — Reporting, Export, Backup, and Restore

### Phase 4.1 — JSON Backup and Restore

Completed:

- versioned `benchpup_archive` JSON format
- atomic UTF-8 archive export
- preview with no writes
- transactional merge restore
- relationship ID remapping
- exact-duplicate skipping
- safe replace restore through a temporary database
- automatic pre-restore safety backup
- migrations and foreign-key validation before final swap
- archive validation and round-trip tests

**Status: Complete**

### Phase 4.1 Polish

Completed:

- default backups to `<project_root>/backups/`
- create the folder automatically
- remove temporary console diagnostics
- add polished backup and restore summaries

### Phase 4.2 — Markdown Reports and Leaderboards

Completed:

- UI-independent reporting engine foundation and typed report models
- detailed benchmark-run and historical scoreboard Markdown reports
- deterministic model leaderboards
- session reports with score, review-level, speed, and run summaries
- hardware reports grouped by authoritative historical hardware snapshots
- immutable Concise, Standard, and Full Audit report templates
- session-local template and inclusion options with privacy-sensitive defaults
- screen-based Reports workflows, previews, destination autocomplete,
  explicit confirmation, overwrite handling, and structured write statuses

**Status: Complete**


### Phase 4.2A — Reporting Engine Foundation

Completed:

- `ReportingService` selection and aggregation boundary
- typed benchmark, scoreboard, leaderboard, session, and hardware reports
- deterministic summaries and historical snapshot authority
- UI-independent Markdown renderers
- staged UTF-8 Markdown writer with explicit overwrite protection

### Phase 4.2B — Reporting CLI Integration

Completed:

- Reports screen navigation for detailed runs, scoreboard history, and
  leaderboards
- vertical catalog selectors and snapshot-text filters
- structured previews, path autocomplete, confirmation, and write-result screens
- session-local options with no new database persistence

### Phase 4.2C — Session, Hardware, and Report Templates

Completed:

- dedicated session report workflow with catalog session selection
- dedicated hardware report workflow with shared filters and optional detail
  sections
- immutable built-in report templates with isolated applied options
- privacy-sensitive prompt, raw-output, and attachment metadata controls

Persisted custom report-template editing remains future work; the existing
`ExportProfile` model is intentionally unchanged.


### Phase 4.3 — JSONL Dataset Builder

Completed:

- reusable DatasetBuilder engine API
- curated JSONL v1 export from detailed BenchmarkRun records only
- eligibility, warnings, filters, duplicate accounting, and redaction
- preview, build, explicit overwrite confirmation, and existing-dataset validation
- companion manifest generation and dataset/manifest pair verification
- staged output and structured write results
- screen-based, session-local Dataset Builder configuration

### Phase 4.4 — Statistics and Comparison

Next:

- descriptive statistics
- model comparisons
- session comparison
- trend reports
- richer HTML charts

## Phase 5 — GUI

Implement a PySide6 dark-mode desktop application only after the reporting,
statistics, and model-comparison engine/reporting phases are complete. It will
reuse the existing engine and design language.

## Phase 6 — Advanced Research Features

Planned:

- knowledge base
- structured findings
- AI lab notebook
- advanced search
- prompt library improvements
- benchmark templates
- trends
- plugins
