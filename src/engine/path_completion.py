"""Path parsing and optional readline completion for CLI prompts."""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys
from typing import Callable


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


def install_path_completion(extensions: tuple[str, ...] = (), *, debug: Callable[[str], None] | None = None) -> Callable[[], None]:
    """Install a temporary readline completer; return a function that restores it."""
    def report(message: str) -> None:
        if debug:
            debug(f"Path completion debug: {message}")

    terminal = (
        "Windows Terminal" if os.environ.get("WT_SESSION") else
        os.environ.get("TERM_PROGRAM") or os.environ.get("TERM") or
        os.environ.get("ComSpec", "unknown terminal")
    )
    report(f"platform={sys.platform}; stdin_tty={sys.stdin.isatty()}; terminal={terminal}")
    try:
        import readline  # type: ignore[import-not-found]
        report(f"imported readline from {getattr(readline, '__file__', 'built-in')}")
    except ImportError as error:
        report(f"readline import failed: {error!r}")
        try:
            import pyreadline3 as readline  # type: ignore[import-not-found]
            report(f"imported pyreadline3 from {getattr(readline, '__file__', 'built-in')}")
        except ImportError as fallback_error:
            report(f"pyreadline3 import failed: {fallback_error!r}; completion unavailable")
            return lambda: None

    try:
        previous_completer = readline.get_completer()
        previous_delimiters = readline.get_completer_delims()
    except (AttributeError, RuntimeError) as error:
        report(f"readline backend does not expose completion APIs: {error!r}")
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
        report("set_completer() and set_completer_delims() executed")
        readline.parse_and_bind("tab: complete")
        report("parse_and_bind('tab: complete') executed")
    except (AttributeError, RuntimeError) as error:
        report(f"Tab binding failed: {error!r}")
        readline.set_completer(previous_completer)
        readline.set_completer_delims(previous_delimiters)
        return lambda: None

    def restore() -> None:
        readline.set_completer(previous_completer)
        readline.set_completer_delims(previous_delimiters)
        report("previous completer restored")

    return restore
