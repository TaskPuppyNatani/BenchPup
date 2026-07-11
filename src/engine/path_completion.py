"""Path parsing helpers used by prompt_toolkit-backed CLI prompts."""
from __future__ import annotations

import os
from pathlib import Path
import re


def unquote_path(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def is_windows_path(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith(r"\\"))


def normalize_path(value: str, *, base_dir: str | Path | None = None) -> str:
    """Remove optional quotes, expand ~, and make local relative paths absolute."""
    cleaned = os.path.expandvars(os.path.expanduser(unquote_path(value)))
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
