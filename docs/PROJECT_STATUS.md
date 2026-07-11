# Project Status

## Current Version

**0.4.0 Alpha**

## Current Test Status

**57 passing**

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

## In Progress

- [ ] Phase 4.1 polish
- [ ] use `<project_root>/backups/` as the default destination
- [ ] automatically create the backups folder
- [ ] remove temporary path-completion and hardware-import diagnostics
- [ ] add polished backup and restore completion summaries

## Next

- [ ] Markdown reports
- [ ] Leaderboards
- [ ] Session reports
- [ ] Hardware reports

## Planned

- [ ] JSONL dataset builder
- [ ] Statistics
- [ ] Charts
- [ ] Benchmark comparison
- [ ] Session comparison
- [ ] PySide6 GUI
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