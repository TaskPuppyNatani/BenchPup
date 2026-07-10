# Changelog

All notable changes to BenchPup will be documented in this file.

## [v0.4.1-alpha] - 2026-07-10

### Added
- Global QA (Quit All) command
- prompt_toolkit-based path completion
- Improved CLI navigation documentation

### Improved
- Windows path prompt reliability
- Backup/restore workflow
- Restore summary formatting
- Help screen navigation

### Fixed
- Windows path prompt rendering
- Path completion lifecycle
- CLI navigation consistency

### Quality
- 69 passing unit tests
- 0 Pyright errors / warnings

## [0.4.0-alpha] - 2026-07-10

### Added
- Versioned JSON backup and restore
- Archive preview
- Transactional merge restore
- Safe replace restore
- Automatic pre-restore safety backups

### Improved
- Hardware profile import
- Documentation
- Interactive HTML reporting
- Windows path autocomplete

### Fixed
- UTF-16 MSInfo32 parsing
- Pylance type issues
- Hardware profile parsing reliability

## [0.3.6-alpha]

### Added
- Hardware Profile import
- MSInfo32 parser
- DXDiag parser
- lshw parser

### Fixed
- CSV validation improvements
- Hardware import duplicate handling

## [0.3.5-alpha]

### Added
- Interactive HTML scoreboard
- Search
- Filters
- Sorting
- Expandable rows