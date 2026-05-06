"""
attendance_parser.py — Teams Attendance Report CSV Parser

Microsoft Teams saves an attendance report alongside each recording in OneDrive.

Actual Teams file format (confirmed from hex dump):
  - Encoding  : UTF-16 LE with BOM (FF FE)
  - Separator : TAB  (not comma)
  - Structure :
      1. Summary
      Meeting title    <tab> Daily Huddle (Mandatory)
      Attended participants <tab> 15
      Meeting start time   <tab> ...
      ...
      2. Participants
      Full Name  <tab> Email  <tab> Joined  <tab> Left  <tab> Duration  <tab> Role
      John Smith <tab> j@co   <tab> 10:01   <tab> 10:45 <tab> 00:44     <tab> Presenter
      ...

Public API:
  find_attendance_csv(recording_path)  → str | None
  parse_attendance_csv(file_path)      → AttendanceReport | None
  classify_attendance(whitelist, csv_attendees, transcript_speakers) → Dict
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class AttendanceReport:
    attendees: List[str]    # Sorted, deduplicated names who were present
    source_file: str
    total_count: int = 0


# ──────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────

def find_attendance_csv(recording_path: str) -> Optional[str]:
    """
    Search the same folder as the recording for a Teams attendance CSV.

    Priority:
      1. Name contains 'attendance' (Teams default naming pattern)
      2. Name starts with 'meetingattendancereport'
      3. Any CSV sharing significant words with the recording filename
    """
    recording = Path(recording_path)
    directory = recording.parent
    base_words = {
        w.lower()
        for w in re.split(r"[\s\-_()\[\]]+", recording.stem)
        if len(w) > 3
    }

    candidates: List[tuple] = []
    try:
        for csv_file in directory.glob("*.csv"):
            lower = csv_file.name.lower()
            if "attendance" in lower:
                candidates.append((0, str(csv_file)))
            elif lower.startswith("meetingattendancereport"):
                candidates.append((1, str(csv_file)))
            elif base_words and any(w in lower for w in base_words):
                candidates.append((2, str(csv_file)))
    except PermissionError:
        logger.warning("Permission denied reading directory: %s", directory)
        return None

    if not candidates:
        logger.debug("No attendance CSV found alongside: %s", recording.name)
        return None

    candidates.sort(key=lambda x: x[0])
    found = candidates[0][1]
    logger.info("Found attendance CSV: %s", Path(found).name)
    return found


def parse_attendance_csv(file_path: str) -> Optional[AttendanceReport]:
    """
    Parse a Teams attendance CSV and return the list of attendees.
    Returns None if the file cannot be parsed or yields no valid names.
    """
    try:
        names = _extract_names(file_path)
    except Exception as exc:
        logger.error("Failed to parse attendance CSV %s: %s", file_path, exc)
        return None

    if not names:
        logger.warning("Attendance CSV has no valid names: %s", Path(file_path).name)
        return None

    attendees = sorted(names)
    logger.info(
        "Attendance CSV parsed: %d attendee(s) from %s",
        len(attendees), Path(file_path).name,
    )
    return AttendanceReport(
        attendees=attendees,
        source_file=file_path,
        total_count=len(attendees),
    )


def classify_attendance(
    whitelist: List[str],
    csv_attendees: Optional[List[str]],
    transcript_speakers: List[str],
) -> Dict:
    """
    Classify each whitelisted participant into one of four buckets.

    With CSV:
      spoke   = in CSV AND spoke in transcript
      silent  = in CSV AND muted (not in transcript)
      absent  = in whitelist AND not in CSV

    Without CSV:
      spoke   = confirmed present (spoke in transcript)
      unknown = cannot confirm — do NOT label as absent
    """
    result: Dict = {
        "has_csv": csv_attendees is not None,
        "spoke":   [],
        "silent":  [],
        "absent":  [],
        "unknown": [],
    }

    norm_speakers = {_norm(n) for n in transcript_speakers}

    if csv_attendees is not None:
        norm_csv = {_norm(n) for n in csv_attendees}
        for name in whitelist:
            n = _norm(name)
            if n in norm_csv and n in norm_speakers:
                result["spoke"].append(name)
            elif n in norm_csv:
                result["silent"].append(name)
            else:
                result["absent"].append(name)
    else:
        for name in whitelist:
            if _norm(name) in norm_speakers:
                result["spoke"].append(name)
            else:
                result["unknown"].append(name)

    return result


# ──────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────

_SKIP_VALUES = frozenset({
    "full name", "name", "participant", "meeting start", "meeting end",
    "n/a", "na", "", "organizer", "role", "email",
})

# Columns that are definitely NOT names
_NON_NAME_PATTERN = re.compile(
    r"^\d|@|http|participant|meeting|duration|joined|left|title|summary|start|end",
    re.IGNORECASE
)


def _norm(name: str) -> str:
    return name.strip().lower()


def _extract_names(file_path: str) -> Set[str]:
    """
    Parse the UTF-16 LE tab-separated Teams attendance file.

    File structure:
      Section "1. Summary" — metadata key/value rows
      Section "2. Participants" — header row + one row per attendee
    """
    content = _read_utf16(file_path)
    lines = [line.rstrip() for line in content.splitlines()]

    # ── Find the "2. Participants" section ────────────────────
    participants_start = _find_participants_section(lines)

    if participants_start is not None:
        return _parse_participant_table(lines, participants_start)
    else:
        # Fallback: scan all lines for valid name cells
        logger.debug("'2. Participants' section not found — using full-file scan")
        return _fallback_scan(lines)


def _find_participants_section(lines: List[str]) -> Optional[int]:
    """
    Return the line index immediately after the '2. Participants' heading.
    Returns None if the section is not found.
    """
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Matches "2. Participants", "2.Participants", "Participants" headings
        if re.match(r"^2\.?\s*participants?$", stripped, re.IGNORECASE):
            return i + 1  # Header row follows
    return None


def _parse_participant_table(lines: List[str], start_idx: int) -> Set[str]:
    """
    Parse the header + data rows of the Participants section.
    Expected header: Full Name\tEmail\tJoined\tLeft\tDuration\tRole
    """
    names: Set[str] = set()

    if start_idx >= len(lines):
        return names

    header_line = lines[start_idx].strip()
    # Handle both tab and comma separators
    sep = "\t" if "\t" in header_line else ","
    headers = [h.strip().lower() for h in header_line.split(sep)]

    # Find the name column index
    name_col = None
    for candidate in ("full name", "name", "participant"):
        if candidate in headers:
            name_col = headers.index(candidate)
            break

    if name_col is None:
        # Try first column as a fallback
        name_col = 0

    for line in lines[start_idx + 1:]:
        if not line.strip():
            continue
        # Stop at next section heading
        if re.match(r"^\d+\.", line.strip()):
            break

        cells = line.split(sep)
        if name_col < len(cells):
            name = cells[name_col].strip()
        else:
            name = cells[0].strip() if cells else ""

        if _is_valid_name(name):
            names.add(name)

    return names


def _fallback_scan(lines: List[str]) -> Set[str]:
    """
    When section markers are missing, scan every tab-cell for valid names.
    Only picks cells from the first column of each line (most likely names).
    """
    names: Set[str] = set()
    for line in lines:
        if not line.strip():
            continue
        # Take only the first tab-separated cell
        first_cell = line.split("\t")[0].strip()
        if _is_valid_name(first_cell):
            names.add(first_cell)
    return names


def _is_valid_name(value: str) -> bool:
    """Return True if value looks like a real person's name."""
    if not value:
        return False
    if _norm(value) in _SKIP_VALUES:
        return False
    if _NON_NAME_PATTERN.match(value):
        return False
    # Must start with a capital letter and contain only name-like characters
    return bool(re.match(r"^[A-Z][A-Za-z\s.\-']{1,60}$", value))


def _read_utf16(file_path: str) -> str:
    """
    Read a Teams attendance file — tries UTF-16 (with BOM) first,
    then falls back to common encodings for non-Teams CSVs.
    """
    for encoding in ("utf-16", "utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(file_path, "r", encoding=encoding, newline="") as f:
                content = f.read()
            # Sanity check: reject if content looks like binary garbage
            if content and content[0] not in ("\ufeff", "\n", "\r", "1", "2", "F", "M"):
                continue
            return content
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError(f"Cannot decode attendance file: {file_path}")
