# Project Status

## Current Version

**0.4.1 Alpha**

## Current Test Status

**145 passing**

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

## Next

- [ ] Reporting enhancements and Markdown reports
- [ ] Statistics
- [ ] Leaderboards
- [ ] Model comparisons
- [ ] Session and hardware reports

## Planned

- [ ] Richer HTML charts
- [ ] Benchmark comparison
- [ ] Session comparison
- [ ] PySide6 GUI after reporting and statistics engine phases
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
