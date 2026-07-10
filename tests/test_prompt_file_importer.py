import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine.prompt_file_importer import PromptFileError, decode_prompt_file, prompt_preview


class PromptFileImporterTests(unittest.TestCase):
    def test_plain_text_is_preserved_exactly(self):
        text = "Keep every word.\n\n  Indentation stays.\n"
        decoded, encoding = decode_prompt_file(text.encode("utf-8"))
        self.assertEqual(decoded, text)
        self.assertEqual(encoding, "UTF-8")
        self.assertEqual(hashlib.sha256(decoded.encode("utf-8")).hexdigest(), hashlib.sha256(text.encode("utf-8")).hexdigest())

    def test_markdown_is_preserved_exactly(self):
        text = "# Review\n\n- first\n- second\n\n```python\n  return value\n```\n"
        decoded, _ = decode_prompt_file(text.encode("utf-8"))
        self.assertEqual(decoded, text)

    def test_utf8_bom_is_removed_without_changing_text(self):
        decoded, encoding = decode_prompt_file(b"\xef\xbb\xbfPrompt body\n")
        self.assertEqual(decoded, "Prompt body\n")
        self.assertEqual(encoding, "UTF-8 with BOM")

    def test_utf16_bom_is_decoded(self):
        text = "Prompt body\n  with indentation"
        decoded, encoding = decode_prompt_file(text.encode("utf-16"))
        self.assertEqual(decoded, text)
        self.assertEqual(encoding, "UTF-16")

    def test_empty_and_binary_files_are_rejected(self):
        with self.assertRaisesRegex(PromptFileError, "empty"):
            decode_prompt_file(b"")
        with self.assertRaisesRegex(PromptFileError, "binary"):
            decode_prompt_file(b"prompt\x00body")

    def test_preview_limits_display_without_changing_source(self):
        text = "\n".join(f"line {number}" for number in range(12))
        preview, truncated = prompt_preview(text)
        self.assertTrue(truncated)
        self.assertEqual(preview, "\n".join(f"line {number}" for number in range(10)) + "\n")
