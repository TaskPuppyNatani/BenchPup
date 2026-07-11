# BenchPup Architecture

# Architecture Principles

## Core Engine First

BenchPup supports multiple user interfaces.

The Core Engine is the single source of truth for all business logic.

The CLI, GUI, and any future interfaces (Web, API, scripting, etc.) must reuse
the Core Engine rather than implementing their own logic.

Responsibilities are divided as follows:

### Core Engine

Responsible for:

- Validation
- CRUD operations
- Repository access
- Statistics
- Reports
- Imports
- Exports
- Backup
- Restore
- Dataset generation
- Business rules

The Core Engine must not depend on any specific user interface.

### CLI

Responsible only for:

- Screen rendering
- Keyboard navigation
- Menus
- User prompts
- Progress display

The CLI must never duplicate business logic.

### GUI

Responsible only for:

- Windows
- Dialogs
- Widgets
- Tables
- Charts
- Drag & Drop
- Visualization

The GUI must never duplicate business logic.

### Future Interfaces

Future interfaces such as a Web UI, REST API, or scripting interface should
also call the Core Engine rather than implementing their own logic.

## Design Goal

Every feature should be implemented once in the Core Engine.

The CLI and GUI are two different front ends over the same engine.

Adding a new interface should require little more than a new presentation layer.


## Domain Data Classes

```text
ModelProfile
HardwareProfile
BenchmarkSession
BenchmarkDefinition
PromptTemplate
ReviewScore
BenchmarkRun
RunAttachment
ScoreboardImportBatch
ScoreboardEntry
ExportProfile
```

`BenchmarkRun` owns model, prompt, benchmark, and hardware snapshots.
`ScoreboardEntry` is a first-class historical summary record grouped through
`ScoreboardImportBatch`.

## Engine Boundaries

```text
CLI / future PySide6 GUI
        |
        +-- Application services
        |     Sessions, Profiles, Definitions, Templates, Hardware
        |     Runs, Scores, Attachments, Scoreboard, Export, Archive
        |     Search, Statistics
        |
        +-- Import subsystem
        |     CSV mapping/import
        |     Hardware parser registry
        |       MSInfo32
        |       DXDiag
        |       lshw --short
        |
        +-- Reporting subsystem
        |     CSV, Markdown, JSONL, standalone HTML
        |
        +-- Archive subsystem
              Versioned JSON export
              Validation and preview
              Transactional merge
              Safe replace
              Pre-restore safety backup

                    |
            Repositories + migrations
                    |
                  SQLite
```

## Import Architecture

CSV import decodes supported encodings, normalizes headings, detects file type,
auto-maps columns, previews records, validates transactionally, handles
duplicates, and commits or rolls back.

Hardware import uses a parser registry. Each parser exposes `source_name`,
`can_parse(text)`, and `parse(text) -> HardwareProfileDraft`.

## Archive Architecture

Export gathers all first-class entities, writes a temporary UTF-8 JSON file,
validates it, and atomically replaces the destination.

Preview performs no writes.

Merge restore remaps IDs in dependency order and rolls back on failure.

Replace restore creates a safety backup, restores into a temporary database,
runs migrations and foreign-key checks, opens it through the normal database
layer, and swaps only after all checks succeed.
