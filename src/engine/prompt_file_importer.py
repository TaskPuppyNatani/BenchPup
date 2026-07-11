"""Safe decoding helpers for raw prompt-template files."""
from __future__ import annotations


class PromptFileError(ValueError):
    """A readable prompt file could not be safely decoded."""


def decode_prompt_file(raw: bytes) -> tuple[str, str]:
    """Decode a text prompt without changing any decoded prompt characters."""
    if not raw:
        raise PromptFileError("Prompt file is empty")
    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text, encoding = raw.decode("utf-16"), "UTF-16"
        elif raw.startswith(b"\xef\xbb\xbf"):
            text, encoding = raw.decode("utf-8-sig"), "UTF-8 with BOM"
        else:
            text, encoding = raw.decode("utf-8"), "UTF-8"
    except UnicodeDecodeError:
        raise PromptFileError("File is not valid UTF-8 or UTF-16 text") from None
    if "\x00" in text:
        raise PromptFileError("File appears to be binary, not a text prompt")
    if not text:
        raise PromptFileError("Prompt file is empty")
    return text, encoding


def prompt_preview(text: str, *, max_lines: int = 10, max_characters: int = 800) -> tuple[str, bool]:
    """Return a display-only preview without modifying the original prompt text."""
    lines = text.splitlines(keepends=True)
    preview = "".join(lines[:max_lines])
    truncated = len(lines) > max_lines
    if len(preview) > max_characters:
        preview = preview[:max_characters]
        truncated = True
    return preview, truncated
