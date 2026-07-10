# Local LLM Benchmark Recorder roadmap

## Phase 0 — Architecture

Define the database schema, domain models, import/export contracts, CLI flow,
GUI wireframe, and training dataset contract. No new application code belongs
in this phase.

## Phase 1 — Core engine

Build SQLite migrations/versioning and UI-independent CRUD services for
sessions, model profiles, benchmark definitions, prompt templates, hardware
profiles, runs, review scores, and run attachments. Add search and statistics.
Undo/redo remains planned, but must not block the basic CRUD workflow.

## Phase 2 — CLI

Add the complete interactive menu on top of the engine.

## Phase 3 — Import

Import Google Sheets CSV exports with field mapping, preview, and duplicate
detection.

## Phase 4 — Export

Add CSV, Markdown, JSON, JSONL, leaderboard, and curated training exports.

## Phase 5 — GUI

Implement a PySide6 dark-mode dashboard with recent runs, statistics, charts,
and a leaderboard.

## Phase 6 — Advanced

Add profiles, prompt libraries, benchmark templates, comparisons, trends,
dataset builder, and plugins.
