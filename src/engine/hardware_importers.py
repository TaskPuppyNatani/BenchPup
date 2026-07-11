"""Pluggable parsers for hardware inventory exports."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


def decode_hardware_text(raw: bytes) -> str:
    """Decode common inventory exports, including MSInfo32 UTF-16 text files."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if b"\x00" in raw[:512]:
        return raw.decode("utf-16-le" if raw[1::2].count(0) >= raw[0::2].count(0) else "utf-16-be")
    return raw.decode("utf-8", errors="replace")


def parse_key_value_pairs(text: str) -> list[tuple[str, str, str]]:
    """Return (section, key, value) tuples from colon or tab-separated exports."""
    section, pairs = "", []
    for line in text.lstrip("\ufeff").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            continue
        if "\t" in line:
            key, value = line.split("\t", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        key, value = key.strip(), value.strip()
        if key and value:
            pairs.append((section, key, value))
    return pairs


def _value(text: str, *labels: str) -> str:
    wanted = {label.casefold() for label in labels}
    for _, key, value in parse_key_value_pairs(text):
        if key.casefold() in wanted:
            return value
    return ""


def _values(text: str, *labels: str) -> list[str]:
    wanted = {label.casefold() for label in labels}
    return [value for _, key, value in parse_key_value_pairs(text) if key.casefold() in wanted]


def _gigabytes(value: str) -> float | None:
    match = re.search(r"([\d.]+)\s*(TB|GB|MB|GIB|MIB)?", value, re.I)
    if not match:
        return None
    amount, unit = float(match.group(1)), (match.group(2) or "GB").upper()
    return amount * (1024 if unit == "TB" else 1 / 1024 if unit in {"MB", "MIB"} else 1)


@dataclass
class HardwareProfileDraft:
    source_name: str
    name: str = ""
    computer_name: str = ""
    cpu: str = ""
    gpu: str = ""
    vram_gb: float | None = None
    ram_gb: float | None = None
    operating_system: str = ""
    backend_versions: dict[str, str] = field(default_factory=dict)
    notes: str = ""


class MSInfo32Parser:
    source_name = "MSInfo32"

    def can_parse(self, text: str) -> bool:
        text = text.lstrip("\ufeff")
        return "System Information" in text and bool(_value(text, "OS Name", "System Name"))

    def parse(self, text: str) -> HardwareProfileDraft:
        text = text.lstrip("\ufeff")
        pairs = parse_key_value_pairs(text)
        def values(*labels: str, section: str | None = None) -> list[str]:
            wanted = {label.casefold() for label in labels}
            return [value for current, key, value in pairs
                    if key.casefold() in wanted and (section is None or current.casefold() == section.casefold())]
        def value(*labels: str) -> str:
            found = values(*labels)
            return found[0] if found else ""
        computer = _value(text, "System Name")
        os_name, version = value("OS Name"), value("Version")
        operating_system = os_name if not version or version in os_name else f"{os_name} {version}"
        gpus = values("Adapter Description", "Graphics Card") + values("Name", section="Display")
        if not gpus:
            gpus = values("Name")
        gpu = " | ".join(dict.fromkeys(item for item in gpus if item))
        drivers = values("Driver Version", section="Display") or values("Driver Version")
        notes = "Display driver version: " + " | ".join(dict.fromkeys(drivers)) if drivers else ""
        cpu = value("Processor").split(",", 1)[0]
        return HardwareProfileDraft(
            self.source_name, name=computer, computer_name=computer, cpu=cpu,
            gpu=gpu, ram_gb=_gigabytes(value("Installed Physical Memory (RAM)")),
            operating_system=operating_system, notes=notes,
        )


class DXDiagParser:
    source_name = "DXDiag"

    def can_parse(self, text: str) -> bool:
        return "DxDiag" in text or ("Machine name:" in text and "Operating System:" in text)

    def parse(self, text: str) -> HardwareProfileDraft:
        computer = _value(text, "Machine name")
        gpus = re.findall(r"(?im)^\s*Card name\s*:\s*(.+?)\s*$", text)
        vram_values = [_gigabytes(value) for value in re.findall(r"(?im)^\s*Display Memory\s*:\s*(.+?)\s*$", text)]
        return HardwareProfileDraft(
            self.source_name, name=computer, computer_name=computer, cpu=_value(text, "Processor"),
            gpu=" | ".join(dict.fromkeys(gpus)), vram_gb=max((value for value in vram_values if value is not None), default=None),
            ram_gb=_gigabytes(_value(text, "Memory")), operating_system=_value(text, "Operating System"),
        )


class LshwShortParser:
    source_name = "lshw --short"

    def can_parse(self, text: str) -> bool:
        return bool(re.search(r"(?im)\b(processor|display|memory)\b", text)) and "/0" in text

    def parse(self, text: str) -> HardwareProfileDraft:
        def row(kind: str) -> str:
            match = re.search(rf"(?im)^\S+\s+{kind}\s+(.+?)\s*$", text)
            return match.group(1).strip() if match else ""
        computer = _value(text, "hostname", "Host")
        memory = row("memory") or _value(text, "size")
        return HardwareProfileDraft(
            self.source_name, name=computer or "Imported Linux hardware", computer_name=computer,
            cpu=row("processor"), gpu=row("display"), ram_gb=_gigabytes(memory),
            operating_system=_value(text, "Operating System", "product"),
        )


class HardwareImporterRegistry:
    def __init__(self, parsers=None):
        self.parsers = {parser.source_name: parser for parser in (parsers or (MSInfo32Parser(), DXDiagParser(), LshwShortParser()))}

    def source_names(self) -> tuple[str, ...]:
        return tuple(self.parsers)

    def parse(self, source_name: str, text: str) -> HardwareProfileDraft:
        parser = self.parsers[source_name]
        return parser.parse(text)
