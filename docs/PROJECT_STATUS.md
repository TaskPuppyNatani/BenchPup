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

## Next

- [ ] PySide6 GUI after the reporting and analytics engine phases

## Planned

Phase 4 is complete. The GUI remains planned and is not complete; it should
reuse the existing engine and reporting boundaries.

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
