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

In progress:

- default backups to `<project_root>/backups/`
- create the folder automatically
- remove temporary console diagnostics
- add polished backup and restore summaries

### Phase 4.2 — Markdown Reports and Leaderboards

Next:

- enhanced Markdown reports
- session reports
- model leaderboards
- hardware summaries
- scoreboard and benchmark-run reports
- report templates

### Phase 4.3 — JSONL Dataset Builder

Planned:

- curated training-data export
- score/verdict filters
- sensitive-text redaction
- duplicate detection
- dataset manifest
- validation and preview

### Phase 4.4 — Statistics and Comparison

Planned:

- descriptive statistics
- model comparison
- session comparison
- trend reports
- richer HTML charts

## Phase 5 — GUI

Implement a PySide6 dark-mode desktop application that reuses the existing
engine and design language.

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
