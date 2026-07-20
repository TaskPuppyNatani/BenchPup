import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cli import TerminalApp
from engine.domain import HardwareProfile
from engine.hardware_importers import (
    DXDiagParser,
    HardwareDecodeAmbiguityError,
    HardwareImportDecodeError,
    HardwareImporterRegistry,
    HardwareParserDetectionError,
    HardwareTextCandidate,
    LshwShortParser,
    MSInfo32Parser,
    UnknownHardwareParserError,
    UnsupportedHardwareEncodingError,
    decode_hardware_text_candidates,
    decode_hardware_text_strict,
    read_hardware_text,
)


class HardwareImporterTests(unittest.TestCase):
    def test_msinfo32_parser(self):
        draft = MSInfo32Parser().parse("System Information\nSystem Name: TASKPUPPY-PC\nOS Name: Microsoft Windows 11 Pro\nProcessor: AMD Ryzen 7 7800X3D\nInstalled Physical Memory (RAM): 64.0 GB\nAdapter Description: AMD Radeon RX 7900 XTX\n")
        self.assertEqual((draft.name, draft.cpu, draft.gpu, draft.ram_gb), ("TASKPUPPY-PC", "AMD Ryzen 7 7800X3D", "AMD Radeon RX 7900 XTX", 64.0))

    def test_msinfo32_parser_supports_bom_and_tab_separated_item_value_export(self):
        sample = (
            "\ufeffSystem Information\n[System Summary]\n"
            "OS Name\tMicrosoft Windows 10 Pro\nVersion\t10.0.19045 Build 19045\n"
            "System Name\tFURFAGDESKTOP\nProcessor\tAMD Ryzen 7 9800X3D\n"
            "Installed Physical Memory (RAM)\t32.0 GB\n[Display]\n"
            "Name\tAMD Radeon RX 7900 XTX\nAdapter RAM\t1,048,576 bytes\nDriver Version\t31.0.24027.1012\n"
        )
        draft = MSInfo32Parser().parse(sample)
        self.assertEqual((draft.name, draft.computer_name, draft.cpu, draft.ram_gb), ("FURFAGDESKTOP", "FURFAGDESKTOP", "AMD Ryzen 7 9800X3D", 32.0))
        self.assertEqual(draft.gpu, "AMD Radeon RX 7900 XTX")
        self.assertIsNone(draft.vram_gb)
        self.assertIn("31.0.24027.1012", draft.notes)
        self.assertIn("Microsoft Windows 10 Pro", draft.operating_system)

    def test_cli_decodes_exact_utf16_msinfo32_tab_export_before_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "furfagdesktop.txt"
            sample = (
                "System Information\n[System Summary]\n"
                "System Name\tFURFAGDESKTOP\nOS Name\tMicrosoft Windows 10 Pro\n"
                "Version\t10.0.19045 Build 19045\n"
                "Processor\tAMD Ryzen 7 9800X3D 8-Core Processor, 4700 Mhz, 8 Core(s), 16 Logical Processor(s)\n"
                "Installed Physical Memory (RAM)\t32.0 GB\n[Display]\nName\tAMD Radeon RX 7900 XTX\n"
            )
            source.write_bytes(b"\xff\xfe" + sample.encode("utf-16-le"))
            answers = iter(["1", str(source), "y"])
            app = TerminalApp(Path(directory) / "bench.db", input_fn=lambda _: next(answers), output_fn=lambda _: None)
            app.import_hardware_profile()
            profile = app.catalog.hardware_profiles.list()[0]
            self.assertEqual((profile.name, profile.computer_name, profile.cpu, profile.gpu, profile.ram_gb, profile.operating_system),
                             ("FURFAGDESKTOP", "FURFAGDESKTOP", "AMD Ryzen 7 9800X3D 8-Core Processor", "AMD Radeon RX 7900 XTX", 32.0,
                              "Microsoft Windows 10 Pro 10.0.19045 Build 19045"))

    def test_dxdiag_parser(self):
        draft = DXDiagParser().parse("DxDiag Notes\nMachine name: DESKTOP\nOperating System: Windows 11 Pro\nProcessor: AMD Ryzen\nMemory: 65536MB RAM\nCard name: NVIDIA RTX\nDisplay Memory: 24576 MB\n")
        self.assertEqual((draft.computer_name, draft.gpu, draft.vram_gb, draft.ram_gb), ("DESKTOP", "NVIDIA RTX", 24.0, 64.0))

    def test_lshw_parser(self):
        draft = LshwShortParser().parse("/0/0 processor AMD Ryzen\n/0/1 memory 32GiB System Memory\n/0/2 display AMD Radeon\nhostname: penguin\n")
        self.assertEqual((draft.name, draft.cpu, draft.gpu, draft.ram_gb), ("penguin", "AMD Ryzen", "AMD Radeon", 32.0))

    def test_registry_returns_parser_candidates_without_file_io(self):
        registry = HardwareImporterRegistry()
        matched = registry.detect_parser_candidates(
            "System Information\nOS Name: Windows 11\nSystem Name: DESKTOP\n"
        )
        self.assertEqual((matched.status, matched.parser_name, matched.candidates), ("matched", "MSInfo32", ("MSInfo32",)))

        ambiguous = registry.detect_parser_candidates(
            "System Information\nOS Name: Windows 11\nMachine name: DESKTOP\nOperating System: Windows 11\n"
        )
        self.assertEqual(ambiguous.status, "ambiguous")
        self.assertEqual(ambiguous.candidates, ("MSInfo32", "DXDiag"))

        unsupported = registry.detect_parser_candidates("plain notes without hardware markers")
        self.assertEqual((unsupported.status, unsupported.candidates), ("unsupported", ()))

    def test_explicit_parser_override_validates_lookup_separately(self):
        registry = HardwareImporterRegistry()
        draft = registry.parse_selected("DXDiag", "Machine name: DESKTOP\nOperating System: Windows 11\n")
        self.assertEqual(draft.source_name, "DXDiag")
        with self.assertRaises(UnknownHardwareParserError):
            registry.parse_selected("Removed parser", "text")

    def test_detection_failure_is_not_reported_as_a_parser_match(self):
        class BrokenParser:
            source_name = "Broken"

            def can_parse(self, _text):
                raise RuntimeError("broken detector")

            def parse(self, _text):
                raise AssertionError("not reached")

        with self.assertRaises(HardwareParserDetectionError):
            HardwareImporterRegistry(parsers=(BrokenParser(),)).detect_parser_candidates("text")

    def test_strict_gui_read_supports_bom_and_reports_decode_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            utf8 = root / "utf8.txt"
            utf8.write_bytes(b"\xef\xbb\xbfSystem Information\n")
            decoded = read_hardware_text(utf8)
            self.assertEqual((decoded.encoding, decoded.text), ("utf-8-sig", "System Information\n"))

            utf8_plain = root / "utf8-plain.txt"
            utf8_plain.write_bytes(b"System Information\n")
            self.assertEqual(read_hardware_text(utf8_plain).text, "System Information\n")

            utf16 = root / "utf16.txt"
            utf16.write_bytes(b"\xff\xfe" + "System Information\n".encode("utf-16-le"))
            self.assertEqual(read_hardware_text(utf16).encoding, "utf-16")

            utf16_be = root / "utf16-be-bom.txt"
            utf16_be.write_bytes(b"\xfe\xff" + "System Information\n".encode("utf-16-be"))
            self.assertEqual(read_hardware_text(utf16_be).encoding, "utf-16")

            invalid = root / "invalid.txt"
            invalid.write_bytes(b"\xff\xfe\x00")
            with self.assertRaises(HardwareImportDecodeError):
                read_hardware_text(invalid)

            utf32 = root / "utf32.txt"
            utf32.write_bytes(b"\xff\xfe\x00\x00" + "hardware".encode("utf-32-le"))
            with self.assertRaises(UnsupportedHardwareEncodingError):
                read_hardware_text(utf32)

    def test_strict_gui_read_rejects_bomless_utf32_and_binary_but_accepts_bomless_utf16(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, encoding in (("utf32-le.txt", "utf-32-le"), ("utf32-be.txt", "utf-32-be")):
                source = root / name
                source.write_bytes("System Information\n".encode(encoding))
                with self.subTest(encoding=encoding):
                    with self.assertRaises(UnsupportedHardwareEncodingError):
                        read_hardware_text(source)

            binary = root / "binary.bin"
            binary.write_bytes(b"\x00\xff\x01\xfe\x02\xfd\x03\xfc")
            with self.assertRaises(UnsupportedHardwareEncodingError):
                read_hardware_text(binary)

            utf16_le = root / "utf16-le.txt"
            utf16_le.write_bytes("System Information\n".encode("utf-16-le"))
            self.assertEqual(read_hardware_text(utf16_le).text, "System Information\n")

            utf16_be = root / "utf16-be.txt"
            utf16_be.write_bytes("System Information\n".encode("utf-16-be"))
            self.assertEqual(read_hardware_text(utf16_be).text, "System Information\n")

    def test_strict_gui_rejects_printable_high_byte_binary(self):
        for raw in (
            b"A\x00\xff\x00B\x00\xfe\x00",
            b"C\x00\xfe\x00D\x00\xfd\x00",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(UnsupportedHardwareEncodingError):
                    decode_hardware_text_candidates(raw)

    def test_strict_gui_rejects_invalid_unicode_and_control_heavy_text(self):
        with self.assertRaises(HardwareImportDecodeError):
            decode_hardware_text_candidates("\ufffd".encode("utf-8"))
        with self.assertRaises(HardwareImportDecodeError):
            decode_hardware_text_candidates("\ufdd0".encode("utf-8"))
        with self.assertRaises(HardwareImportDecodeError):
            decode_hardware_text_candidates(b"\x01\x02\x03")

    def test_strict_gui_edge_payloads_are_typed_and_odd_utf16_is_not_tried(self):
        self.assertEqual(decode_hardware_text_candidates(b"")[0].text, "")
        self.assertEqual(decode_hardware_text_candidates(b"A")[0].encoding, "utf-8")
        odd_candidates = decode_hardware_text_candidates(b"ABC")
        self.assertTrue(all(candidate.encoding == "utf-8" for candidate in odd_candidates))
        with self.assertRaises(UnsupportedHardwareEncodingError):
            decode_hardware_text_candidates(b"\x00" * 8)
        with self.assertRaises(UnsupportedHardwareEncodingError):
            decode_hardware_text_candidates(b"A\x00\x00\x00\x00\x00B\x00")
        with self.assertRaises(UnsupportedHardwareEncodingError):
            decode_hardware_text_candidates(b"A\x00B\x00CDEFGH")

    def test_bomless_non_ascii_utf16_is_kept_as_candidates_until_parser_resolution(self):
        sample = (
            "System Information\nSystem Name: 漢字\n"
            "OS Name: Windows 11\nProcessor: International CPU\n"
        )
        registry = HardwareImporterRegistry()
        for encoding in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                candidates = decode_hardware_text_candidates(sample.encode(encoding))
                resolution = registry.resolve_decode_candidates(candidates)
                self.assertEqual(resolution.status, "matched")
                self.assertIsNotNone(resolution.selected)
                assert resolution.selected is not None
                self.assertEqual(resolution.selected.candidate.encoding, encoding)

    def test_ambiguous_decode_candidates_require_typed_resolution(self):
        class MarkerParser:
            source_name = "Marker"

            def can_parse(self, text):
                return text in {"LE", "BE"}

            def parse(self, text):
                return MSInfo32Parser().parse(text)

        registry = HardwareImporterRegistry(parsers=(MarkerParser(),))
        candidates = (
            HardwareTextCandidate(encoding="utf-8", text="LE"),
            HardwareTextCandidate(encoding="utf-16-le", text="BE"),
        )
        resolution = registry.resolve_decode_candidates(candidates)
        self.assertEqual(resolution.status, "ambiguous")
        self.assertEqual(len(resolution.matches), 2)

        explicit = registry.resolve_parser_candidates("Marker", candidates[:1])
        self.assertEqual(explicit.status, "matched")
        self.assertEqual(explicit.selected.candidate.encoding, "utf-8")  # type: ignore[union-attr]
        with self.assertRaises(HardwareDecodeAmbiguityError):
            decode_hardware_text_strict("漢字".encode("utf-16-le"))

    def test_hardware_import_preview_cancel_and_create(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "msinfo.txt"
            source.write_text("System Information\nSystem Name: TASKPUPPY-PC\nOS Name: Windows 11\nProcessor: Ryzen\nInstalled Physical Memory (RAM): 64 GB\n", encoding="utf-8")
            output = []
            answers = iter(["1", str(source), "c"])
            app = TerminalApp(Path(directory) / "bench.db", input_fn=lambda _: next(answers), output_fn=output.append)
            app.import_hardware_profile()
            self.assertEqual(app.catalog.hardware_profiles.list(), [])
            self.assertTrue(any("Detected Hardware Profile" in line for line in output))

            answers = iter(["1", str(source), "y"])
            app.input = lambda _: next(answers)
            app.import_hardware_profile()
            profile = app.catalog.hardware_profiles.list()[0]
            self.assertEqual((profile.name, profile.import_source), ("TASKPUPPY-PC", "MSInfo32"))

    def test_duplicate_hardware_import_updates_existing_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "msinfo.txt"
            source.write_text("System Information\nSystem Name: TASKPUPPY-PC\nProcessor: New CPU\n", encoding="utf-8")
            answers = iter(["1", str(source), "y", "1"])
            app = TerminalApp(Path(directory) / "bench.db", input_fn=lambda _: next(answers), output_fn=lambda _: None)
            app.catalog.hardware_profiles.create(HardwareProfile(name="TASKPUPPY-PC", cpu="Old CPU"))
            app.import_hardware_profile()
            profiles = app.catalog.hardware_profiles.list()
            self.assertEqual((len(profiles), profiles[0].cpu), (1, "New CPU"))
