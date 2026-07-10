# Local LLM Benchmark Recorder

A local, dependency-free CLI for recording LLM coding benchmarks. Results live in
SQLite and can be exported as CSV or JSONL training examples.

## Quick start

```powershell
python src/main.py
```

Choose an action from the interactive menu. The database defaults to
`data/benchmark.db`; exports go to `data/exports/`.
-----------------------------------------------------------------------------------------------------
# Local LLM Benchmark Recorder

A desktop-first benchmark recording tool for evaluating local LLMs on coding and code-review tasks.

## Features

- SQLite database
- Interactive CLI
- Sessions
- Model profiles
- Hardware profiles
- Prompt templates
- Review scoring
- Attachments
- CSV import (planned)
- JSONL export (planned)
- PySide6 GUI (planned)

## Current Status

Version 0.2 Alpha

Completed:
- Phase 0
- Phase 1
- Phase 2
- CLI Polish

Current Development:
Phase 3 - CSV Import

## Requirements

Python 3.11+

## Running

python src/main.py

## Tests

python -m unittest discover -s tests -v