"""Path parsing and optional readline completion for CLI prompts."""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Callable


_last_module_name = "unavailable"
_tab_complete_bound = False


def unquote_path(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def is_windows_path(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith(r"\\"))


def normalize_path(value: str, *, base_dir: str | Path | None = None) -> str:
    """Remove optional quotes, expand ~, and make local relative paths absolute."""
    cleaned = os.path.expanduser(unquote_path(value))
    # Preserve Windows paths when tests or tooling run on a non-Windows host.
    if os.name != "nt" and is_windows_path(cleaned):
        return cleaned
    path = Path(cleaned)
    if not path.is_absolute() and base_dir is not None:
        path = Path(base_dir) / path
    return str(path.resolve())


def resolve_export_destination(destination: str | Path, *, default_filename: str, extension: str | None = None) -> Path:
    """Turn a user-selected export destination into a concrete output file path."""
    raw = str(destination)
    has_trailing_separator = raw.endswith(("/", "\\"))
    path = Path(raw)
    if has_trailing_separator or path.is_dir():
        path /= default_filename
    if extension and not path.suffix:
        path = path.with_suffix(extension)
    return path


def path_candidates(value: str, *, extensions: tuple[str, ...] = (), directories_only: bool = False) -> list[str]:
    """Return filesystem completion candidates without relying on terminal state."""
    raw = unquote_path(value)
    expanded = os.path.expanduser(raw)
    path = Path(expanded)
    parent = path.parent if str(path.parent) else Path(".")
    prefix = path.name
    try:
        children = sorted(parent.iterdir(), key=lambda child: (not child.is_dir(), child.name.lower()))
    except OSError:
        return []
    allowed = {extension.lower() for extension in extensions}
    candidates = []
    for child in children:
        if not child.name.lower().startswith(prefix.lower()):
            continue
        if directories_only and not child.is_dir():
            continue
        if not child.is_dir() and allowed and child.suffix.lower() not in allowed:
            continue
        candidate = str(child)
        if child.is_dir():
            candidate += os.sep
        if " " in candidate:
            candidate = f'"{candidate}"'
        candidates.append(candidate)
    return candidates


def install_path_completion(extensions: tuple[str, ...] = ()) -> Callable[[], None]:
    """Install a temporary readline completer; return a function that restores it."""
    global _last_module_name, _tab_complete_bound
    _tab_complete_bound = False
    try:
        import readline  # type: ignore[import-not-found]
    except ImportError:
        try:
            import pyreadline3 as readline  # type: ignore[import-not-found]
        except ImportError:
            return lambda: None
    _last_module_name = getattr(readline, "__name__", type(readline).__name__)

    try:
        previous_completer = readline.get_completer()
        previous_delimiters = readline.get_completer_delims()
    except (AttributeError, RuntimeError):
        return lambda: None
    matches: list[str] = []

    def complete(_: str, state: int) -> str | None:
        nonlocal matches
        if state == 0:
            matches = path_candidates(readline.get_line_buffer(), extensions=extensions)
        return matches[state] if state < len(matches) else None

    try:
        readline.set_completer(complete)
        # Spaces and path separators must remain part of the current completion token.
        readline.set_completer_delims("\t\n")
        readline.parse_and_bind("tab: complete")
        _tab_complete_bound = True
    except (AttributeError, RuntimeError):
        readline.set_completer(previous_completer)
        readline.set_completer_delims(previous_delimiters)
        return lambda: None

    def restore() -> None:
        readline.set_completer(previous_completer)
        readline.set_completer_delims(previous_delimiters)

    return restore


def completion_diagnostics() -> tuple[str, object | None, str | None, bool]:
    """Temporary runtime details for validating the active completion backend."""
    try:
        import readline  # type: ignore[import-not-found]
    except ImportError:
        try:
            import pyreadline3 as readline  # type: ignore[import-not-found]
        except ImportError:
            return _last_module_name, None, None, _tab_complete_bound
    try:
        return (
            getattr(readline, "__name__", type(readline).__name__),
            readline.get_completer(),
            readline.get_completer_delims(),
            _tab_complete_bound,
        )
    except (AttributeError, RuntimeError):
        return getattr(readline, "__name__", type(readline).__name__), None, None, _tab_complete_bound
