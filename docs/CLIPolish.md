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
- [x] Global Quit All command (QA)

Q remains context-specific. QA always exits the entire application and works
from menus, submenus, wizards, confirmations, path prompts, import/export,
backup, and restore flows. Cleanup/finally blocks run before exit. Escape is not
currently supported or documented as a cancellation command.

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

- [X] Confirm temporary path-completion diagnostics are removed
- [x] Remove temporary hardware-import diagnostics from normal CLI output
- [x] Keep technical details in logs when needed
- [x] Default backups to `<project_root>/backups/`
- [ ] Automatically create the backups folder
- [ ] Confirm polished backup summary
- [x] Show aligned, human-readable restore summary

### Navigation

- [x] Back from every submenu
- [x] Cancel current operation
- [x] Review before save
- [x] Confirmation before delete
- [x] Global Quit All (QA)
