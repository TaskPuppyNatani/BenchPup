# Project Status

## Current Version

**0.4.1 Alpha**

## Current Test Status

**282 passing**

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
- [x] Phase 5B Runs browsing and Add Run workflow
- [x] Phase 5C1 Sessions, Model Profiles, and Benchmark Definitions catalog GUI
- [x] Phase 5C2 Prompt Templates and Hardware Profiles catalog GUI

Phase 5B adds a read-only Runs browser with search and filters, complete
historical Run Details, and a review-before-save Add Run wizard. Catalog
selectors use current service records, manual/custom relationships remain
nullable where the engine permits them, and no catalog records are created
implicitly. Snapshots, fingerprints, validation, timestamps, and atomic
run-plus-review creation remain engine-owned. A successful save refreshes Runs
and Dashboard.

Phase 5C1 adds typed engine catalog operations and functional Sessions, Models,
and Benchmarks pages. Sessions support active/archived/all visibility and
archive/restore; Model Profiles support explicit editing and default
management; and Benchmark Definitions support active/inactive/all visibility,
editing, and deactivate/reactivate. Catalog editors preserve nullable values,
use explicit 24-hour local timestamp entry with system-timezone/DST-aware UTC
serialization, and preserve run snapshots. Add Run reloads eligible selectors
after a catalog change without creating catalog records implicitly.

Phase 5C2 adds functional Prompt Templates and Hardware Profiles pages. Prompt
Template editors preserve exact multiline text, derive hashes through the engine,
and support active/inactive lifecycle changes. Hardware Profile editors preserve
optional numeric values, structured backend-version mappings, and imported
provenance metadata. Historical prompt and hardware snapshots remain unchanged
after reusable catalog edits.

Run editing/deletion, import/export dialogs, report dialogs, Dataset Builder
forms, packaging, and installers are not implemented in the GUI.

## Next

- [ ] Remaining Phase 5 GUI workflow slices

Phase 4 is complete and Phase 5 is in progress. Remaining GUI workflow slices
are intentionally deferred. The CLI remains a permanent first-class interface
and coexists with the PySide6 GUI over the same engine and database boundaries.

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
