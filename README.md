# BenchPup

BenchPup is a local, desktop-first benchmark recorder for evaluating LLM coding
and code-review tasks. Detailed benchmark runs, historical scoreboard entries,
profiles, prompts, and reviews are stored in SQLite.

## Current Status

Version 0.4.1 Alpha.

Completed:

- interactive screen-based CLI
- CSV and hardware-profile imports
- scoreboard browsing and standalone HTML reporting
- JSON backup and restore
- curated JSONL Dataset Builder with filters, redaction, manifests, and validation

Next: reporting enhancements, statistics, leaderboards, and model comparisons.
The PySide6 GUI follows those engine/reporting phases.

## Quick Start

```powershell
python src/main.py
```

Choose an action from the interactive menu. Use the Dataset Builder to preview,
build, and validate curated JSONL datasets from detailed BenchmarkRun records.

## Requirements

Python 3.11+

## Running

python src/main.py

## Tests

python -m unittest discover -s tests -v
