# BenchPup

BenchPup is a local, desktop-first benchmark recorder for evaluating LLM coding
and code-review tasks. Detailed benchmark runs, historical scoreboard entries,
profiles, prompts, and reviews are stored in SQLite.

## Current Status

Version 0.4.1 Alpha.

Completed:

- interactive screen-based CLI
- CSV and hardware-profile imports
- scoreboard browsing, legacy HTML viewing, and standalone HTML analytics
- JSON backup and restore
- curated JSONL Dataset Builder with filters, redaction, manifests, and validation
- descriptive statistics, model/session comparisons, and UTC trend reports

Phase 4 reporting and analytics are complete. Phase 5 is in progress: the
PySide6 desktop shell now includes a read-only Dashboard, Runs browser, run
details, review-before-save Add Run workflow, and functional Sessions, Models,
and Benchmarks catalog pages over the existing engine boundaries. The CLI
remains a permanent first-class interface.

## Quick Start

```powershell
python src/main.py
```

Choose an action from the interactive menu. Use the Dataset Builder to preview,
build, and validate curated JSONL datasets from detailed BenchmarkRun records.

To launch the Phase 5 desktop shell:

```powershell
python -m src.gui
```

The GUI uses the same `data/benchmark.db` and `config/settings.json` location
resolution as the CLI. Runs browsing supports free-text search, model,
benchmark, session, and scored/unscored filters. Run Details is read-only,
and Add Run supports existing catalog selections or manual/custom entry,
optional review scores, engine-owned snapshots and fingerprints, and atomic
run-plus-review creation. Dashboard and Runs refresh after a successful save.
Sessions support active/archived/all visibility and archive/restore; Models
support explicit profile editing and engine-owned default management; and
Benchmarks support active/inactive/all visibility, editing, and lifecycle
changes, and Add Run shows the selected definition's stored file or target,
benchmark type, and default prompt. Prompt resolution preserves explicit run
text first, then a selected PromptTemplate, then the definition default.
Prompt Templates support exact multiline editing, engine-owned hashes,
and active/inactive lifecycle management. Hardware Profiles support structured
backend-version editing and imported provenance preservation. Catalog edits
preserve stored run snapshots, and Add Run refreshes its eligible selectors
after catalog changes. Run editing/deletion, imports/exports, reports, and
other workflow forms remain future GUI slices.

## Requirements

Python 3.11+

pyreadline3>=3.5.4; sys_platform == "win32"

prompt_toolkit>=3.0.0

PySide6>=6.8

## Running

python src/main.py

python -m src.gui

## Tests

python -m unittest discover -s tests -v

# Windows PowerShell / offscreen GUI test mode
$env:QT_QPA_PLATFORM = "offscreen"
python -m unittest discover -s tests
