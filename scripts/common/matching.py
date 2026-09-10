"""
Shared matching/filtering helpers used by every sync script in this repo.

The only link between ASM and DaySmart is the ASM shelter code embedded in
the DaySmart patient name, e.g. "Biscuit - A2024001". Every script matches
records this way -- there is no other shared ID between the two systems.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# ASM shelter codes look like A2024001, D2025012, etc. -- one uppercase
# letter + 7-8 digits. Confirmed against real ASM data across this project.
ASM_CODE_PATTERN = re.compile(r"\s*-\s*([A-Z]\d{4,})\s*$")
ASM_CODE_VALID = re.compile(r"^[A-Z]\d{7,8}$")

# The clinic is in Arizona, which does not observe DST -- fixed UTC-7 year
# round. DaySmart returns timestamps in UTC; taking the calendar date
# without converting first rolls anything recorded in the Arizona evening
# (roughly 5pm onward) into the next day.
_ARIZONA_TZ = timezone(timedelta(hours=-7))


def _to_arizona(dt: datetime) -> datetime:
    """Convert an offset-aware datetime to Arizona local time. Offset-naive
    values (ASM's own date fields have no tzinfo) pass through unchanged."""
    return dt.astimezone(_ARIZONA_TZ) if dt.tzinfo else dt


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
    """MM/DD/YYYY -- the format ASM's csv_import expects for date columns.

    Converts to Arizona local time first -- see _to_arizona().
    """
    dt = parse_date(value)
    return _to_arizona(dt).strftime("%m/%d/%Y") if dt else ""


# DaySmart's 'sex' field combines sex + altered-status into one of 5 values
# -- there is no separate boolean like ASM's NEUTERED. ids confirmed from
# live DaySmart data: 1=Male (intact), 2=Male (neutered), 3=Female (intact),
# 4=Female (spayed), 5=Unknown. ASM has no distinct "spayed" concept --
# SEXNAME (Male/Female/Unknown) + the NEUTERED boolean cover every DaySmart
# value with one gap: ASM's "Unknown, NEUTERED=1" has no DaySmart equivalent
# (DaySmart's "Unknown" carries no altered-status info) and falls back to
# plain Unknown below.
DS_SEX_ID: dict[tuple[str, bool], int] = {
    ("male", False): 1,
    ("male", True): 2,
    ("female", False): 3,
    ("female", True): 4,
}
DS_SEX_LABEL = {1: "Male (intact)", 2: "Male (neutered)", 3: "Female (intact)", 4: "Female (spayed)", 5: "Unknown"}
DS_SEX_ALTERED_IDS = {2, 4}  # DaySmart sex ids that mean "already spayed/neutered"
DS_SEX_UNKNOWN_ID = 5


def map_sex_from_asm(sexname: str, neutered: int) -> int:
    """ASM's SEXNAME + NEUTERED -> DaySmart's combined sex id."""
    s = (sexname or "").strip().lower()
    n = neutered == 1
    if "female" in s:
        return DS_SEX_ID[("female", n)]
    if "male" in s:
        return DS_SEX_ID[("male", n)]
    return DS_SEX_UNKNOWN_ID


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
    # Convert to Arizona local time before dropping tzinfo, so an
    # offset-aware value from DaySmart ("...+00:00") lands on the same
    # calendar date ASM would have recorded it under, then drop tzinfo so it
    # can be subtracted against an offset-naive value from ASM's side
    # ("YYYY-MM-DD..."). Losing time-of-day precision doesn't matter at
    # multi-day tolerance.
    da, db = _to_arizona(da).replace(tzinfo=None), _to_arizona(db).replace(tzinfo=None)
    return abs((da - db).days) <= tolerance_days
