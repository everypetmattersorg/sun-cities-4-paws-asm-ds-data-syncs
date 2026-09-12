"""
DaySmart to ASM Vaccination Data Sync
------------------------------------
Pulls given vaccinations from DaySmart /reminders (item.type == "Vaccinations",
givenDate populated) and writes them to the matching ASM shelter animal via
csv_import.

CRITICAL LESSON FROM THIS PROJECT'S HISTORY -- ROOT CAUSE CONFIRMED: writing
a vaccination type string that doesn't exactly match a real ASM
vaccinationtype.VaccinationType name doesn't leave the field unset -- ASM3's
real csv_import source (src/asm3/csvimport.py on GitHub) shows an unmatched
VACCINATIONTYPE falls back to `asm3.configuration.default_vaccination_type()`,
a shelter-configured system default (apparently set to a "combo" type on
this shelter's ASM account), which is exactly how ~100 records got
mislabeled previously. To prevent a repeat, this script:
  1. Reads ASM's live vaccinationtype table via a custom SQL report (see
     ASM_VACCINATION_TYPES_REPORT_TITLE / SETUP below) every run, rather
     than trusting a hardcoded ID/name list that can drift out of date.
  2. Only sends a row when DaySmart's reminder item.label has a confident
     normalized match against one of those real names. A reminder with no
     confident match is SKIPPED and logged for manual review -- never sent
     under a best-guess/default type, so ASM's own default-type fallback
     above never gets triggered by this script.

FIELD NAME BUG FOUND AND FIXED (2026-09-06): the given-date column is
VACCINATIONGIVENDATE, not VACCINATIONDATE -- also confirmed against the
real csv_import source. An earlier version of this script sent
"VACCINATIONDATE", which ASM's importer does not recognize.

DATE MAPPING BUG FOUND AND FIXED (2026-09-12) -- confirmed against ASM3's
real csvimport.py: VACCINATIONDUEDATE maps to animalvaccination.DateRequired,
NOT DateExpires, and VACCINATIONEXPIRESDATE (a column this script never
sent) is what maps to DateExpires. At this shelter, DateRequired must
always equal DateOfVaccination (the shot was given -- there's nothing left
"required" about it), and DateExpires is the actual next-due date.
DaySmart's own "due" field (dueDate) is that same next-due-date concept,
i.e. it corresponds to ASM's DateExpires, not DateRequired. The previous
version of this script sent DaySmart's dueDate as VACCINATIONDUEDATE,
which silently wrote it into DateRequired instead (wrong field) while
never populating DateExpires at all (left blank). Every row this script
writes now sets VACCINATIONDUEDATE to the given date itself and
VACCINATIONEXPIRESDATE to DaySmart's dueDate.

ISACTIVE BUG FIX (found during the field-mapping review, 2026-09-04): a
DaySmart reminder can be superseded/cancelled (isActive: false, with a
deleteAt timestamp) while a replacement reminder for the same vaccine
exists alongside it. Only isActive: true reminders are synced -- the old
data-integrity script did not check this and would have synced cancelled
duplicate rows.

DUPLICATE CHECK: before writing, this script reads ASM's existing
vaccination records via ASM_VACCINATION_REPORT_TITLE (see SETUP) and skips
any DaySmart reminder that already has a matching ASM record (same animal,
same resolved type, given date within 1 day). If EITHER required ASM
report can't be read, nothing is written this run -- writing blind without
a working duplicate check is exactly what this script exists to prevent.

SETUP: two custom SQL reports must exist in ASM (Reports -> Add report,
SQL/Advanced type, no criteria) with these exact titles:

  "Vaccinations (All Time)"  (ASM_VACCINATION_REPORT_TITLE)
    SELECT a.ShelterCode AS ShelterCode, a.AnimalName AS AnimalName,
           vt.VaccinationType AS VaccinationType,
           av.DateOfVaccination AS DateGiven, av.DateRequired AS DateRequired,
           av.DateExpires AS DateExpires, av.Comments AS Comments
    FROM animalvaccination av
    INNER JOIN animal a ON a.ID = av.AnimalID
    LEFT OUTER JOIN vaccinationtype vt ON vt.ID = av.VaccinationID
    WHERE av.DateOfVaccination Is Not Null
    ORDER BY a.ShelterCode

  "Vaccination Types (All)"  (ASM_VACCINATION_TYPES_REPORT_TITLE)
    SELECT ID, VaccinationType FROM vaccinationtype ORDER BY VaccinationType

If either report is missing/misnamed, this script logs why and writes
nothing that run (see DUPLICATE CHECK above).

Matching: the ASM shelter code embedded in the DaySmart patient name (e.g.
"Biscuit - A2024001"). Deceased/adopted/inactive animals are excluded on
both sides (see common/asm.py and common/daysmart.py).

Run (dry run, default):  python3 scripts/daysmart_to_asm_vaccination_sync.py
Run (live):               python3 scripts/daysmart_to_asm_vaccination_sync.py --live
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from common import asm, daysmart
from common.matching import dates_close, fmt_date_for_asm
from common.report import send_sync_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

REPORT_TO = [addr.strip() for addr in os.environ.get("VACCINATION_SYNC_REPORT_TO", "").split(",") if addr.strip()]

FLOW_NAME = "DaySmart to ASM Vaccination Data Sync"

ASM_VACCINATION_REPORT_TITLE = "Vaccinations (All Time)"
ASM_VACCINATION_TYPES_REPORT_TITLE = "Vaccination Types (All)"

GIVEN_DATE_TOLERANCE_DAYS = 1


def _ci_get(row: dict, *names: str):
    upper = {str(k).upper(): v for k, v in row.items()}
    for name in names:
        if name.upper() in upper:
            return upper[name.upper()]
    return None


def _rows_to_csv(rows: list[dict]) -> str:
    """Render a list of same-shaped row dicts as CSV text, for log review --
    mirrors the [DRY RUN] CSV preview already logged for rows to write, so
    the skipped-as-duplicate rows are just as easy to pull into a
    spreadsheet for a human to review."""
    if not rows:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def normalise_vax_name(s: str) -> str:
    """
    Strip DaySmart's trailing '*' and normalize whitespace/case/wording for
    matching. Applied to both sides (DaySmart labels and ASM type names), so
    it's safe regardless of which side spells it out. Two wording variations
    confirmed via real dry runs against live data:
      - DaySmart uses "Rabies 1 year"/"Rabies 3 year"; ASM's vaccinationtype
        table uses the abbreviated "Rabies 1 yr"/"Rabies 3 yr".
      - ASM's FVRCP/FELV combo dose/year entries were created as "FVRCP/FELV
        COMBO #1" etc.; DaySmart's reminder item labels are "FVRCP/FELV #1"
        etc., with no "combo" in them.
    Neither is a typo, just a different convention on each side -- safe to
    fold together. An actual misspelling (e.g. ASM's "FVRVP #2" instead of
    "FVRCP #2") is NOT handled here; that needs a real fix in ASM's data,
    not a fuzzy-match guess (see this project's ~100-record vaccination
    mislabeling history for why guessing at a type match is dangerous).
    """
    normalised = re.sub(r"\s+", " ", (s or "").strip().rstrip("*").strip()).lower()
    normalised = re.sub(r"\byears?\b", "yr", normalised)
    normalised = re.sub(r"\bcombo\b", "", normalised)
    return re.sub(r"\s+", " ", normalised).strip()


def load_asm_vaccination_types() -> dict[str, str] | None:
    """normalized name -> real ASM VaccinationType name (exact string to write)."""
    raw = asm.get_report(ASM_VACCINATION_TYPES_REPORT_TITLE)
    if raw is None:
        return None
    types: dict[str, str] = {}
    for row in raw:
        name = (_ci_get(row, "VaccinationType") or "").strip()
        if name:
            types[normalise_vax_name(name)] = name
    log.info("ASM: %d vaccination type(s) loaded for name matching.", len(types))
    return types


def load_asm_existing_vaccinations() -> dict[str, list[dict]] | None:
    """SHELTERCODE -> list of {type, given, due, ...} already recorded in ASM."""
    raw = asm.get_report(ASM_VACCINATION_REPORT_TITLE)
    if raw is None:
        return None
    by_code: dict[str, list[dict]] = {}
    for row in raw:
        code = (_ci_get(row, "ShelterCode") or "").strip()
        if not code:
            continue
        by_code.setdefault(code, []).append({
            "type": (_ci_get(row, "VaccinationType") or "").strip(),
            "given": _ci_get(row, "DateGiven"),
            "due": _ci_get(row, "DateRequired"),
        })
    log.info("ASM: existing vaccination records loaded for %d animal(s).", len(by_code))
    return by_code


def already_in_asm(existing: list[dict], vax_type: str, given_date: str) -> bool:
    for rec in existing:
        if rec["type"].strip().lower() != vax_type.strip().lower():
            continue
        if dates_close(rec.get("given"), given_date, GIVEN_DATE_TOLERANCE_DAYS):
            return True
    return False


def build_rows(
    token: str,
    patients: list[dict],
    asm_names: dict[str, str],
    asm_existing_vax: dict[str, list[dict]],
    asm_vax_types: dict[str, str],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (rows_to_write, skipped_duplicates, skipped_no_type_match)."""
    patients_by_id = {p["id"]: p for p in patients}
    raw = daysmart.paginate(token, "reminders")

    rows, skipped_dup, skipped_no_match = [], [], []

    for r in raw:
        item = r.get("item") or {}
        if (item.get("type") or "").lower() != "vaccinations":
            continue
        given_date = r.get("givenDate", "")
        if not given_date:
            continue
        if not r.get("isActive", False):
            continue  # cancelled/superseded reminder -- see ISACTIVE BUG FIX above

        patient_id = (r.get("patient") or {}).get("id", "")
        patient = patients_by_id.get(patient_id)
        if not patient:
            continue
        code = patient["asm_code"]
        if code not in asm_names:
            continue

        ds_label = item.get("label", "")
        matched_type = asm_vax_types.get(normalise_vax_name(ds_label))
        if not matched_type:
            skipped_no_match.append({
                "ANIMALCODE": code, "ANIMALNAME": asm_names[code],
                "DaySmartLabel": ds_label, "GivenDate": fmt_date_for_asm(given_date),
            })
            continue

        row = {
            "ANIMALCODE": code,
            "ANIMALNAME": asm_names[code],
            "VACCINATIONTYPE": matched_type,
            "VACCINATIONGIVENDATE": fmt_date_for_asm(given_date),
            # VACCINATIONDUEDATE maps to ASM's DateRequired, which must equal
            # the given date once the shot has actually been given (see DATE
            # MAPPING BUG note above) -- NOT DaySmart's own "due" date.
            "VACCINATIONDUEDATE": fmt_date_for_asm(given_date),
            # VACCINATIONEXPIRESDATE maps to ASM's DateExpires -- the real
            # next-due date, which is what DaySmart's dueDate represents.
            "VACCINATIONEXPIRESDATE": fmt_date_for_asm(r.get("dueDate", "")),
            "VACCINATIONCOMMENTS": r.get("note", ""),
        }

        if already_in_asm(asm_existing_vax.get(code, []), matched_type, given_date):
            skipped_dup.append(row)
            continue

        rows.append(row)

    log.info(
        "Vaccination rows to write: %d (skipped %d already in ASM, %d with no confident type match)",
        len(rows), len(skipped_dup), len(skipped_no_match),
    )
    if skipped_no_match:
        log.warning(
            "Vaccination labels with no confident ASM type match (add/rename in ASM's "
            "vaccinationtype table, or fix the DaySmart inventory item label, then re-run): %s",
            sorted({s["DaySmartLabel"] for s in skipped_no_match}),
        )
    return rows, skipped_dup, skipped_no_match


def main():
    parser = argparse.ArgumentParser(description=FLOW_NAME)
    parser.add_argument("--live", action="store_true", help="Actually write to ASM. Default is dry run.")
    args = parser.parse_args()

    log.info("=== %s started at %s (live=%s) ===", FLOW_NAME, datetime.now(timezone.utc).isoformat(), args.live)

    token = daysmart.get_token()
    patients = daysmart.get_active_patients_with_asm_code(token)

    animals = asm.get_shelter_animals()
    asm_names = asm.get_animal_names(animals)

    asm_vax_types = load_asm_vaccination_types()
    asm_existing_vax = load_asm_existing_vaccinations()

    if asm_vax_types is None or asm_existing_vax is None:
        log.error(
            "Could not read one or both required ASM reports -- refusing to write any "
            "vaccination data this run (writing without a working duplicate/type check "
            "is exactly what this script exists to prevent). See SETUP in the docstring."
        )
        send_sync_report(FLOW_NAME, [], REPORT_TO, dry_run=not args.live, send_failed=True)
        log.info("=== %s aborted at %s ===", FLOW_NAME, datetime.now(timezone.utc).isoformat())
        return

    rows, skipped_dup, skipped_no_match = build_rows(token, patients, asm_names, asm_existing_vax, asm_vax_types)

    if skipped_dup:
        log.info("Skipped -- already in ASM (%d):\n%s", len(skipped_dup), _rows_to_csv(skipped_dup))

    ok = asm.csv_import(rows, FLOW_NAME, args.live)
    if args.live:
        asm.post_sync_cleanup(dry_run=False)

    send_sync_report(
        FLOW_NAME,
        rows if ok else [],
        REPORT_TO,
        dry_run=not args.live,
        send_failed=not ok,
        skipped_duplicates=skipped_dup + skipped_no_match,
    )

    log.info("=== %s complete at %s ===", FLOW_NAME, datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    main()
