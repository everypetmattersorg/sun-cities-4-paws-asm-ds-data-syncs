"""
Shared matching/filtering helpers used by every sync script in this repo.

The only link between ASM and DaySmart is the ASM shelter code embedded in
the DaySmart patient name, e.g. "Biscuit - A2024001". Every script matches
records this way -- there is no other shared ID between the two systems.
"""

from __future__ import annotations

import re
from datetime import datetime

# ASM shelter codes look like A2024001, D2025012, etc. -- one uppercase
# letter + 7-8 digits. Confirmed against real ASM data across this project.
ASM_CODE_PATTERN = re.compile(r"\s*-\s*([A-Z]\d{4,})\s*$")
ASM_CODE_VALID = re.compile(r"^[A-Z]\d{7,8}$")


def normalise(s: str | None) -> str:
    return (s or "").strip().lower().replace(" & ", " and ")


def extract_asm_code(patient_name: str | None) -> str | None:
    """Extract the ASM shelter code from a DaySmart patient name like 'Biscuit - A2024001'."""
    m = ASM_CODE_PATTERN.search(patient_name or "")
    return m.group(1) if m else None


def excluded_reason(animal: dict) -> str | None:
    """
    Return a reason string if this ASM animal is deceased or otherwise
    archived (adopted, reclaimed, transferred, etc.), else None.

    json_shelter_animals already filters WHERE Archived=0 server-side
    (confirmed against ASM3's open-source get_shelter_animals()), so this is
    a defensive second check on real field names, not the primary filter.
    """
    if (animal.get("DECEASEDDATE") or "").strip():
        return f"deceased ({animal['DECEASEDDATE']})"
    if animal.get("ARCHIVED"):
        return "archived (adopted/reclaimed/transferred/etc.)"
    return None


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def fmt_date_for_asm(value: str | None) -> str:
    """MM/DD/YYYY -- the format ASM's csv_import expects for date columns."""
    dt = parse_date(value)
    return dt.strftime("%m/%d/%Y") if dt else ""


def dates_close(a: str | None, b: str | None, tolerance_days: int) -> bool:
    """
    True if two date strings represent the same calendar date within
    `tolerance_days`. Shelter animal DOBs are often estimates, and re-entry
    can shift a date by a day due to timezone rounding -- a small tolerance
    avoids treating that as a real conflict.
    """
    da, db = parse_date(a), parse_date(b)
    if da is None or db is None:
        return False
    # Drop tzinfo so an offset-aware value from one side (DaySmart's
    # "...+00:00") doesn't crash subtracting against an offset-naive value
    # from the other (ASM's plain "YYYY-MM-DD..."). Losing time-of-day
    # precision doesn't matter at multi-day tolerance.
    da, db = da.replace(tzinfo=None), db.replace(tzinfo=None)
    return abs((da - db).days) <= tolerance_days
