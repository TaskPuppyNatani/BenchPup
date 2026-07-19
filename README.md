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

Phase 4 reporting and analytics are complete. Phase 5A is in progress: the
initial PySide6 desktop shell and read-only Dashboard reuse the existing
engine/reporting boundaries. The CLI remains a permanent first-class
interface.

## Quick Start

```powershell
python src/main.py
```

Choose an action from the interactive menu. Use the Dataset Builder to preview,
build, and validate curated JSONL datasets from detailed BenchmarkRun records.

To launch the Phase 5A desktop shell:

```powershell
python -m src.gui
```

The GUI uses the same `data/benchmark.db` and `config/settings.json` location
resolution as the CLI. Phase 5A provides navigation, a read-only Dashboard,
and honest placeholders; CRUD forms and workflow dialogs are not implemented
yet.

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
