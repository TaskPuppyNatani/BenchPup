import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cli import TerminalApp
from engine.domain import HardwareProfile
from engine.hardware_importers import DXDiagParser, LshwShortParser, MSInfo32Parser


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
