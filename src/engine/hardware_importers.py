"""Pluggable parsers for hardware inventory exports."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


HardwareParserDetectionStatus = Literal["matched", "ambiguous", "unsupported"]
HardwareDecodeStatus = Literal["matched", "ambiguous", "unsupported"]


class HardwareImportReadError(ValueError):
    """A hardware report could not be read from disk."""


class HardwareImportDecodeError(ValueError):
    """A hardware report uses bytes that cannot be decoded safely."""

    def __init__(self, encoding: str, reason: str = "could not be decoded"):
        self.encoding = encoding
        self.reason = reason
        super().__init__(f"Hardware report encoding {encoding} {reason}")


class UnsupportedHardwareEncodingError(ValueError):
    """A hardware report uses an encoding outside the supported set."""

    def __init__(self, encoding: str):
        self.encoding = encoding
        super().__init__(f"Hardware report encoding {encoding} is not supported")


class UnknownHardwareParserError(ValueError):
    """An explicit parser override is not registered."""

    def __init__(self, source_name: str):
        self.source_name = source_name
        super().__init__(f"Hardware parser {source_name!r} is not available")


class HardwareParseError(ValueError):
    """A selected hardware parser could not parse the decoded report."""

    def __init__(self, source_name: str):
        self.source_name = source_name
        super().__init__(f"The {source_name} parser could not parse this hardware report")


class HardwareParserDetectionError(ValueError):
    """A registered parser could not evaluate a hardware report candidate."""

    def __init__(self, source_name: str):
        self.source_name = source_name
        super().__init__(f"The {source_name} parser could not inspect this hardware report")


@dataclass(frozen=True)
class HardwareTextCandidate:
    """One strictly decoded interpretation of a hardware report."""

    encoding: str
    text: str
    authoritative: bool = False


@dataclass(frozen=True)
class HardwareTextRead:
    """Decoded hardware candidates kept separate from parser candidate detection."""

    path: Path
    candidates: tuple[HardwareTextCandidate, ...]

    @property
    def text(self) -> str:
        if len(self.candidates) != 1:
            raise HardwareDecodeAmbiguityError(tuple(candidate.encoding for candidate in self.candidates))
        return self.candidates[0].text

    @property
    def encoding(self) -> str:
        if len(self.candidates) != 1:
            raise HardwareDecodeAmbiguityError(tuple(candidate.encoding for candidate in self.candidates))
        return self.candidates[0].encoding


class HardwareDecodeAmbiguityError(ValueError):
    """A strict decode has more than one valid interpretation."""

    def __init__(self, encodings: tuple[str, ...]):
        self.encodings = encodings
        choices = ", ".join(encodings) or "multiple encodings"
        super().__init__(f"The hardware report has ambiguous valid decodings: {choices}")


@dataclass(frozen=True)
class HardwareParserDetection:
    """Engine-owned parser candidate information for GUI workflows."""

    status: HardwareParserDetectionStatus
    parser_name: str | None = None
    candidates: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class HardwareDecodeParserMatch:
    """A decoded text candidate matched by one or more hardware parsers."""

    candidate: HardwareTextCandidate
    detection: HardwareParserDetection


@dataclass(frozen=True)
class HardwareDecodeResolution:
    """Parser-backed resolution of one or more decoded text candidates."""

    status: HardwareDecodeStatus
    candidates: tuple[HardwareTextCandidate, ...] = ()
    selected: HardwareDecodeParserMatch | None = None
    matches: tuple[HardwareDecodeParserMatch, ...] = ()
    reason: str = ""


def _is_unicode_noncharacter(codepoint: int) -> bool:
    return 0xFDD0 <= codepoint <= 0xFDEF or codepoint & 0xFFFF in {0xFFFE, 0xFFFF}


def _validate_decoded_text(text: str, encoding: str) -> None:
    """Reject decoded values that cannot safely represent report text."""

    if "\ufffd" in text:
        raise HardwareImportDecodeError(encoding, "contains replacement characters")

    suspicious_count = 0
    for character in text:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise HardwareImportDecodeError(encoding, "contains unpaired surrogates")
        if _is_unicode_noncharacter(codepoint):
            raise HardwareImportDecodeError(encoding, "contains Unicode noncharacters")
        category = unicodedata.category(character)
        if category == "Cc" and character not in "\t\n\r":
            raise HardwareImportDecodeError(encoding, "contains disallowed control characters")
        if category in {"Co", "Cn"}:
            suspicious_count += 1

    if suspicious_count and (
        len(text) < 8 or suspicious_count / max(1, len(text)) >= 0.2
    ):
        raise HardwareImportDecodeError(
            encoding,
            "contains an implausibly high concentration of private-use or unassigned code points",
        )


def _looks_binary_like(candidate: HardwareTextCandidate, raw: bytes) -> bool:
    """Reject easy binary false positives without rejecting ordinary international text."""

    if candidate.authoritative or candidate.encoding not in {"utf-16-le", "utf-16-be"}:
        return False
    if b"\x00" not in raw[:512] or not candidate.text:
        return False
    non_ascii = sum(ord(character) > 127 for character in candidate.text)
    has_report_structure = any(
        character in " \t\n\r:[]()=|/\\;,"
        for character in candidate.text
    )
    return non_ascii >= 2 and non_ascii / len(candidate.text) >= 0.25 and not has_report_structure


def _decode_candidate(raw: bytes, encoding: str, *, authoritative: bool) -> HardwareTextCandidate:
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as error:
        raise HardwareImportDecodeError(encoding) from error
    _validate_decoded_text(text, encoding)
    candidate = HardwareTextCandidate(encoding=encoding, text=text, authoritative=authoritative)
    if _looks_binary_like(candidate, raw):
        raise UnsupportedHardwareEncodingError("binary data")
    return candidate


def decode_hardware_text_candidates(raw: bytes) -> tuple[HardwareTextCandidate, ...]:
    """Strictly decode all plausible GUI interpretations without choosing one silently."""

    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        raise UnsupportedHardwareEncodingError("utf-32")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return (_decode_candidate(raw, "utf-16", authoritative=True),)
    if raw.startswith(b"\xef\xbb\xbf"):
        return (_decode_candidate(raw, "utf-8-sig", authoritative=True),)

    encodings = ["utf-8"]
    if len(raw) % 2 == 0:
        encodings.extend(("utf-16-le", "utf-16-be"))

    candidates: list[HardwareTextCandidate] = []
    errors: list[ValueError] = []
    for encoding in encodings:
        try:
            candidate = _decode_candidate(raw, encoding, authoritative=False)
        except (HardwareImportDecodeError, UnsupportedHardwareEncodingError) as error:
            errors.append(error)
            continue
        if all(existing.text != candidate.text for existing in candidates):
            candidates.append(candidate)

    if candidates:
        return tuple(candidates)
    if b"\x00" in raw[:512]:
        raise UnsupportedHardwareEncodingError("binary data")
    if errors:
        raise errors[0]
    raise HardwareImportDecodeError("unknown")


def decode_hardware_text(raw: bytes) -> str:
    """Decode common inventory exports, including MSInfo32 UTF-16 text files."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if b"\x00" in raw[:512]:
        return raw.decode("utf-16-le" if raw[1::2].count(0) >= raw[0::2].count(0) else "utf-16-be")
    return raw.decode("utf-8", errors="replace")


def decode_hardware_text_strict(raw: bytes) -> tuple[str, str]:
    """Decode a hardware report strictly for GUI preview and import."""

    candidates = decode_hardware_text_candidates(raw)
    if len(candidates) != 1:
        raise HardwareDecodeAmbiguityError(tuple(candidate.encoding for candidate in candidates))
    candidate = candidates[0]
    return candidate.text, candidate.encoding


def read_hardware_text(path: str | Path) -> HardwareTextRead:
    """Read and strictly decode one hardware report without parser matching."""

    try:
        resolved = Path(path).expanduser().resolve(strict=False)
        raw = resolved.read_bytes()
    except (OSError, RuntimeError, ValueError) as error:
        raise HardwareImportReadError("The hardware report could not be read") from error
    return HardwareTextRead(resolved, decode_hardware_text_candidates(raw))


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

    def get_parser(self, source_name: str):
        parser = self.parsers.get(source_name)
        if parser is None:
            raise UnknownHardwareParserError(source_name)
        return parser

    def detect_parser_candidates(self, text: str) -> HardwareParserDetection:
        """Return all parser candidates without performing file I/O or parsing."""

        candidates_list: list[str] = []
        for source_name, parser in self.parsers.items():
            try:
                if parser.can_parse(text):
                    candidates_list.append(source_name)
            except Exception as error:
                raise HardwareParserDetectionError(source_name) from error
        candidates = tuple(candidates_list)
        if len(candidates) == 1:
            return HardwareParserDetection(
                "matched",
                parser_name=candidates[0],
                candidates=candidates,
                reason=f"{candidates[0]} matched the hardware report.",
            )
        if candidates:
            return HardwareParserDetection(
                "ambiguous",
                candidates=candidates,
                reason=f"Multiple hardware parsers matched: {', '.join(candidates)}. Select one explicitly.",
            )
        return HardwareParserDetection(
            "unsupported",
            reason="No supported hardware parser matched the report.",
        )

    @staticmethod
    def _resolution(
        candidates: tuple[HardwareTextCandidate, ...],
        matches: tuple[HardwareDecodeParserMatch, ...],
    ) -> HardwareDecodeResolution:
        if len(matches) == 1 and matches[0].detection.status == "matched":
            match = matches[0]
            return HardwareDecodeResolution(
                "matched",
                candidates=candidates,
                selected=match,
                matches=matches,
                reason=f"{match.candidate.encoding} decoded the report and {match.detection.reason}",
            )
        if not matches:
            return HardwareDecodeResolution(
                "unsupported",
                candidates=candidates,
                reason="No supported hardware parser matched any valid text decoding.",
            )
        if len(matches) == 1 and matches[0].detection.status == "ambiguous":
            return HardwareDecodeResolution(
                "ambiguous",
                candidates=candidates,
                matches=matches,
                reason=matches[0].detection.reason,
            )
        details = "; ".join(
            f"{match.candidate.encoding}: {', '.join(match.detection.candidates)}"
            for match in matches
        )
        return HardwareDecodeResolution(
            "ambiguous",
            candidates=candidates,
            matches=matches,
            reason=f"Multiple credible hardware interpretations remain ({details}). Select a parser explicitly.",
        )

    def resolve_decode_candidates(
        self,
        candidates: tuple[HardwareTextCandidate, ...],
    ) -> HardwareDecodeResolution:
        """Resolve decoded candidates only when parser matching is unambiguous."""

        matches: list[HardwareDecodeParserMatch] = []
        for candidate in candidates:
            detection = self.detect_parser_candidates(candidate.text)
            if detection.candidates:
                matches.append(HardwareDecodeParserMatch(candidate, detection))
        return self._resolution(candidates, tuple(matches))

    def resolve_parser_candidates(
        self,
        source_name: str,
        candidates: tuple[HardwareTextCandidate, ...],
    ) -> HardwareDecodeResolution:
        """Resolve all valid decodings against an explicitly selected parser."""

        parser = self.get_parser(source_name)
        matches: list[HardwareDecodeParserMatch] = []
        for candidate in candidates:
            try:
                matched = parser.can_parse(candidate.text)
            except Exception as error:
                raise HardwareParserDetectionError(source_name) from error
            if matched:
                detection = HardwareParserDetection(
                    "matched",
                    parser_name=source_name,
                    candidates=(source_name,),
                    reason=f"{source_name} matched the hardware report.",
                )
                matches.append(HardwareDecodeParserMatch(candidate, detection))
        return self._resolution(candidates, tuple(matches))

    def parse_selected(self, source_name: str, text: str) -> HardwareProfileDraft:
        """Validate an explicit parser selection and translate parser failures."""

        parser = self.get_parser(source_name)
        try:
            return parser.parse(text)
        except Exception as error:
            raise HardwareParseError(source_name) from error

    def parse(self, source_name: str, text: str) -> HardwareProfileDraft:
        parser = self.parsers[source_name]
        return parser.parse(text)
