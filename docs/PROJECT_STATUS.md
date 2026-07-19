# Project Status

## Current Version

**0.4.1 Alpha**

## Current Test Status

**227 passing**

## Completed

- [x] Architecture
- [x] Core Engine
- [x] SQLite schema and migrations
- [x] CLI
- [x] CLI Polish
- [x] Benchmark Runs CSV import
- [x] Scoreboard CSV import
- [x] CSV auto-detection
- [x] Scoreboard import batches
- [x] Hardware Profile import
- [x] MSInfo32
- [x] DXDiag
- [x] `lshw --short`
- [x] Windows path autocomplete
- [x] Interactive Scoreboard HTML viewer
- [x] JSON backup export
- [x] Backup preview
- [x] Transactional merge restore
- [x] Safe replace restore
- [x] Automatic pre-restore safety backup
- [x] Archive compatibility and validation
- [x] Pylance cleanup
- [x] Phase 4.3 JSONL Dataset Builder
- [x] Dataset eligibility, filters, duplicate accounting, and redaction
- [x] JSONL v1 validation and manifest verification
- [x] Dataset preview, staged build, overwrite confirmation, and validation CLI workflows
- [x] Phase 4.2 reporting engine foundation
- [x] Detailed benchmark, historical scoreboard, and model leaderboard reports
- [x] Session reports and hardware reports from historical run snapshots
- [x] Immutable Concise, Standard, and Full Audit report templates
- [x] Screen-based reporting workflows with staged Markdown writing and overwrite confirmation
- [x] Phase 4.4 descriptive statistics, model/session comparisons, and UTC trend reports
- [x] Typed standalone HTML analytics dashboards for BenchmarkRun and ScoreboardEntry data
- [x] Offline-safe HTML serialization, accessible fallback tables, staged writing, and Export option 6
- [x] Phase 4.1 Polish audit: backup defaults, automatic folder creation, clean diagnostics, and summaries

## Phase 5 GUI

Status: In progress.

- [x] Phase 5A PySide6 application shell
- [x] Shared GUI application context and engine initialization
- [x] Dark-mode shell, keyboard navigation, and centralized theme
- [x] Read-only Dashboard with real engine data
- [x] Navigation placeholders for future GUI slices
- [x] Offscreen GUI tests and smoke coverage
- [ ] Phase 5B Runs browsing and Add Run workflow

Phase 5A is intentionally limited to architecture, navigation, and read-only
Dashboard presentation. CRUD forms, import/export dialogs, report dialogs,
Dataset Builder forms, packaging, and installers are not implemented.

## Next

- [ ] Phase 5B GUI Runs and Add Run workflows

## Planned

Phase 4 is complete and Phase 5 is in progress. The CLI remains a permanent
first-class interface and coexists with the PySide6 GUI over the same engine
and database boundaries.

- [ ] Knowledge base
- [ ] AI lab notebook
- [ ] Plugin system


### CLI

Status: Mature

BenchPup now uses a screen-based CLI with:

- [x] Dedicated screen navigation
- [x] Vertical menus
- [x] Shared renderer
- [x] Global QA navigation
- [x] Full prompt_toolkit integration
- [x] Cross-platform screen redraw
- [x] Consistent screen ownership

The CLI is considered a permanent first-class interface and will coexist with the future PySide6 GUI.
