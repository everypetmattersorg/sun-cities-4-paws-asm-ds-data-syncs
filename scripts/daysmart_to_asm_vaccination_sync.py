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
    SELECT av.ID AS VaccinationID, av.AnimalID AS AnimalID,
           av.VaccinationID AS VaccinationTypeID,
           av.AdministeringVetID AS AdministeringVetID, av.GivenBy AS GivenBy,
           a.ShelterCode AS ShelterCode, a.AnimalName AS AnimalName,
           vt.VaccinationType AS VaccinationType,
           av.DateOfVaccination AS DateGiven, av.DateRequired AS DateRequired,
           av.DateExpires AS DateExpires, av.BatchNumber AS BatchNumber,
           av.BatchExpiryDate AS BatchExpiryDate, av.Manufacturer AS Manufacturer,
           av.RabiesTag AS RabiesTag, av.Cost AS Cost,
           av.CostPaidDate AS CostPaidDate, av.Comments AS Comments
    FROM animalvaccination av
    INNER JOIN animal a ON a.ID = av.AnimalID
    LEFT OUTER JOIN vaccinationtype vt ON vt.ID = av.VaccinationID
    WHERE av.DateOfVaccination Is Not Null
    ORDER BY a.ShelterCode
  (VaccinationID added 2026-09-12 for the one-time cleanup of records
  written before the DATE MAPPING BUG fix above. Every other new column
  added 2026-09-17 so a MATCH-AND-UPDATE (see below) can safely
  round-trip a record through common.asm.update_vaccination() without
  blanking fields it doesn't touch -- ASM3's update endpoint is a
  full-record overwrite, not a partial patch. This script's own
  duplicate check only uses VaccinationType/DateGiven/DateExpires.)

MATCH-AND-UPDATE (added 2026-09-17): previously, a DaySmart reminder that
matched an existing ASM record (same type, given date within 1 day) was
always just skipped, even if that ASM record was missing information
DaySmart has -- most notably DateExpires, which every record written
before the 2026-09-12 fix has blank. Now, a match whose ASM DateExpires
is blank gets enriched via update_vaccination() instead of skipped outright,
filling in DateExpires from DaySmart's dueDate. Nothing else about a
matched record is touched. NOT YET LIVE-VERIFIED: ASM's vaccination-delete
endpoint on this account has failed with a server error on every attempt
regardless of permissions or record; the update endpoint (same
"animal_vaccination" class, different mode) is inferred correct by
analogy to the confirmed-working "animal" endpoint's mode=save, but
carries the same risk of hitting whatever is wrong with this endpoint on
this ASM instance. Test on one real record and verify before trusting it
at scale.

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
    """
    SHELTERCODE -> list of existing ASM vaccination records, each carrying
    every field common.asm.update_vaccination() needs to safely round-trip
    the record (see MATCH-AND-UPDATE in the module docstring) alongside the
    type/given/expires fields the duplicate check itself uses.
    """
    raw = asm.get_report(ASM_VACCINATION_REPORT_TITLE)
    if raw is None:
        return None
    by_code: dict[str, list[dict]] = {}
    for row in raw:
        code = (_ci_get(row, "ShelterCode") or "").strip()
        if not code:
            continue
        by_code.setdefault(code, []).append({
            "vaccination_id": _ci_get(row, "VaccinationID"),
            "animal_id": _ci_get(row, "AnimalID"),
            "type_id": _ci_get(row, "VaccinationTypeID"),
            "administering_vet_id": _ci_get(row, "AdministeringVetID"),
            "given_by": _ci_get(row, "GivenBy"),
            "type": (_ci_get(row, "VaccinationType") or "").strip(),
            "given": _ci_get(row, "DateGiven"),
            "required": _ci_get(row, "DateRequired"),
            "expires": _ci_get(row, "DateExpires"),
            "batch_number": _ci_get(row, "BatchNumber"),
            "batch_expiry": _ci_get(row, "BatchExpiryDate"),
            "manufacturer": _ci_get(row, "Manufacturer"),
            "rabies_tag": _ci_get(row, "RabiesTag"),
            "cost": _ci_get(row, "Cost"),
            "cost_paid_date": _ci_get(row, "CostPaidDate"),
            "comments": _ci_get(row, "Comments"),
        })
    log.info("ASM: existing vaccination records loaded for %d animal(s).", len(by_code))
    return by_code


def find_matching_asm_record(existing: list[dict], vax_type: str, given_date: str) -> dict | None:
    for rec in existing:
        if rec["type"].strip().lower() != vax_type.strip().lower():
            continue
        if dates_close(rec.get("given"), given_date, GIVEN_DATE_TOLERANCE_DAYS):
            return rec
    return None


def build_rows(
    token: str,
    patients: list[dict],
    asm_names: dict[str, str],
    asm_existing_vax: dict[str, list[dict]],
    asm_vax_types: dict[str, str],
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Returns (rows_to_insert, rows_to_enrich, skipped_duplicates, skipped_no_type_match)."""
    patients_by_id = {p["id"]: p for p in patients}
    raw = daysmart.paginate(token, "reminders")

    rows, enrich, skipped_dup, skipped_no_match = [], [], [], []

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

        matched = find_matching_asm_record(asm_existing_vax.get(code, []), matched_type, given_date)
        if matched:
            # See MATCH-AND-UPDATE in the module docstring: a match missing
            # DateExpires (every record from before the 2026-09-12 fix) gets
            # enriched instead of silently skipped. Anything else about an
            # already-matched record is left alone.
            ds_due = r.get("dueDate", "")
            if not str(matched.get("expires") or "").strip() and ds_due:
                enrich.append({
                    "record": matched,
                    "new_expires": fmt_date_for_asm(ds_due),
                    "ANIMALCODE": code, "ANIMALNAME": asm_names[code],
                    "VACCINATIONTYPE": matched_type,
                    "GivenDate": fmt_date_for_asm(given_date),
                    "NewDateExpires": fmt_date_for_asm(ds_due),
                })
            else:
                skipped_dup.append(row)
            continue

        rows.append(row)

    log.info(
        "Vaccination rows to write: %d (skipped %d already in ASM, %d to enrich with a missing DateExpires, "
        "%d with no confident type match)",
        len(rows), len(skipped_dup), len(enrich), len(skipped_no_match),
    )
    if skipped_no_match:
        log.warning(
            "Vaccination labels with no confident ASM type match (add/rename in ASM's "
            "vaccinationtype table, or fix the DaySmart inventory item label, then re-run): %s",
            sorted({s["DaySmartLabel"] for s in skipped_no_match}),
        )
    return rows, enrich, skipped_dup, skipped_no_match


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

    rows, enrich, skipped_dup, skipped_no_match = build_rows(
        token, patients, asm_names, asm_existing_vax, asm_vax_types
    )

    if skipped_dup:
        log.info("Skipped -- already in ASM (%d):\n%s", len(skipped_dup), _rows_to_csv(skipped_dup))

    ok = asm.csv_import(rows, FLOW_NAME, args.live)
    if args.live:
        asm.post_sync_cleanup(dry_run=False)

    enriched, enrich_failed = [], 0
    if enrich:
        report_rows = [{k: v for k, v in u.items() if k != "record"} for u in enrich]
        if not args.live:
            log.info(
                "[DRY RUN] Would enrich %d existing ASM record(s) with a missing DateExpires:\n%s",
                len(enrich), _rows_to_csv(report_rows),
            )
            enriched = report_rows
        else:
            session = asm.login()
            for u in enrich:
                if asm.update_vaccination(session, u["record"], {"expires": u["new_expires"]}):
                    enriched.append({k: v for k, v in u.items() if k != "record"})
                else:
                    enrich_failed += 1
            log.info("Enriched %d / %d existing ASM record(s) with a missing DateExpires.", len(enriched), len(enrich))

    send_sync_report(
        FLOW_NAME,
        rows if ok else [],
        REPORT_TO,
        dry_run=not args.live,
        send_failed=not ok or enrich_failed > 0,
        skipped_duplicates=skipped_dup + skipped_no_match,
    )

    log.info("=== %s complete at %s ===", FLOW_NAME, datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    main()
