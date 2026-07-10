# CLI Polish

## Input

- [x] Case-insensitive commands
- [x] Accept `quit` and `exit`
- [x] Accept `back`
- [x] Validate numeric input
- [x] Never expose `ValueError` to the user
- [x] Blank input keeps defaults where appropriate

## Navigation

- [x] Back from submenus
- [x] Cancel current operation
- [x] Review before save
- [x] Confirmation before destructive actions

## Feedback

- [x] Success messages
- [x] Friendly validation errors
- [x] Pause after informational screens where appropriate
- [x] Unexpected errors logged instead of dumping tracebacks

## Quality of Life

- [x] Remember last-used profile
- [x] Remember last session
- [x] Remember last hardware profile
- [x] Suggest defaults
- [x] Path autocomplete where supported
- [x] Import previews
- [x] Friendly duplicate handling
- [x] Backup preview
- [x] Restore summaries

## Current Polish Task

- [ ] Remove temporary path-completion diagnostics from normal CLI output
- [ ] Remove temporary hardware-import diagnostics from normal CLI output
- [ ] Keep technical details in logs when needed
- [ ] Default backups to `<project_root>/backups/`
- [ ] Automatically create the backups folder
- [ ] Show aligned, human-readable backup and restore summaries
