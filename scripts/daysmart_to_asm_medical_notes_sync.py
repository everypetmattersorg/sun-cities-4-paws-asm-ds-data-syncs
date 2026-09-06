"""
DaySmart to ASM Medical Notes Data Sync
------------------------------------
Pulls billed medical items from DaySmart /invoice-items (medications, labs/
diagnostics, dewormers, treatments, supplements, preventatives -- spay/
neuter is excluded, that's daysmart_to_asm_spay_neuter_sync.py's job) and
writes them to the matching ASM shelter animal via csv_import.

FIELD NAME BUG FOUND AND FIXED (2026-09-06): the date column is
MEDICALGIVENDATE, not MEDICALDATE. Confirmed against ASM3's real
open-source csv_import handler (src/asm3/csvimport.py on GitHub), which
reads MEDICALTYPE/MEDICALNAME/MEDICALDOSAGE/MEDICALGIVENDATE/MEDICALCOMMENTS
and passes them to asm3.medical.insert_regimen_from_form(), writing into
the animalmedical table (TreatmentName, Dosage, StartDate, Comments,
MedicalTypeID, status="2"/completed, singlemulti="0"/single dose). An
earlier version of this script sent "MEDICALDATE", which ASM's importer
does not recognize -- every row would have landed with a blank StartDate.
This project has already been burned once by an unverified field-name
guess (the DaySmart "birthday" vs "birthdate" bug) and once by an
unverified default (~100 vaccination records mislabeled) -- checking the
real source before shipping is exactly what those lessons argue for.

MEDICALTYPE is intentionally still not sent: csv_import resolves it via a
lookup against lksmedicaltype.MedicalTypeName with create=False, and
silently writes MedicalTypeID="0" (i.e. leaves it unset) for anything
missing or unmatched -- confirmed via the same source read. That's a data
gap, not data corruption (unlike the vaccination type bug, which silently
substituted a WRONG real type), so it's left unset here rather than
guessing at ASM's lksmedicaltype names without verifying them the same way.

DUPLICATE CHECK: reads ASM's existing regimen records via
ASM_MEDICAL_REPORT_TITLE (see SETUP below) and skips any DaySmart invoice
item that already has a matching ASM record (same animal, same name, same
date). If that report can't be read, nothing is written this run --
writing blind without a working duplicate check is exactly what this
script exists to prevent. Within a single run, exact repeat items are
also deduped against each other before the ASM check even runs.

SETUP: a custom SQL report must exist in ASM (Reports -> Add report,
SQL/Advanced type, no criteria) with this exact title:

  "Medical Regimens (All Time)"  (ASM_MEDICAL_REPORT_TITLE)
    SELECT
        a.ShelterCode AS ShelterCode,
        a.AnimalName AS AnimalName,
        am.TreatmentName AS MedicalName,
        am.StartDate AS MedicalGivenDate,
        am.Dosage AS MedicalDosage,
        am.Comments AS MedicalComments
    FROM animalmedical am
    INNER JOIN animal a ON a.ID = am.AnimalID
    ORDER BY a.ShelterCode

If it's missing or misnamed, this script logs why and writes nothing that
run.

Matching: the ASM shelter code embedded in the DaySmart patient name (e.g.
"Biscuit - A2024001"). Deceased/adopted/inactive animals are excluded on
both sides (see common/asm.py and common/daysmart.py).

Run (dry run, default):  python3 scripts/daysmart_to_asm_medical_notes_sync.py
Run (live):               python3 scripts/daysmart_to_asm_medical_notes_sync.py --live
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from common import asm, daysmart
from common.matching import fmt_date_for_asm
from common.report import send_sync_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

REPORT_TO = [addr.strip() for addr in os.environ.get("MEDICAL_NOTES_SYNC_REPORT_TO", "").split(",") if addr.strip()]

FLOW_NAME = "DaySmart to ASM Medical Notes Data Sync"

ASM_MEDICAL_REPORT_TITLE = "Medical Regimens (All Time)"

INCLUDE_TYPES = {
    "medication", "medications",
    "laboratory / diagnostics", "labs + diagnostics",
    "dewormer", "dewormers",
    "treatment", "treatments",
    "supplement", "supplements",
    "preventative", "preventatives",
}


def _ci_get(row: dict, *names: str):
    upper = {str(k).upper(): v for k, v in row.items()}
    for name in names:
        if name.upper() in upper:
            return upper[name.upper()]
    return None


def load_asm_existing_medical() -> dict[str, list[dict]] | None:
    """SHELTERCODE -> list of {name, date} regimens already recorded in ASM."""
    raw = asm.get_report(ASM_MEDICAL_REPORT_TITLE)
    if raw is None:
        return None
    by_code: dict[str, list[dict]] = {}
    for row in raw:
        code = (_ci_get(row, "ShelterCode") or "").strip()
        if not code:
            continue
        by_code.setdefault(code, []).append({
            "name": (_ci_get(row, "MedicalName") or "").strip(),
            "date": _ci_get(row, "MedicalGivenDate"),
        })
    log.info("ASM: existing medical regimen records loaded for %d animal(s).", len(by_code))
    return by_code


def build_rows(
    token: str,
    patients: list[dict],
    asm_names: dict[str, str],
    asm_existing_medical: dict[str, list[dict]],
) -> tuple[list[dict], list[dict]]:
    """Returns (rows_to_write, skipped_duplicates)."""
    patients_by_id = {p["id"]: p for p in patients}
    raw = daysmart.paginate(token, "invoice-items")

    rows, skipped = [], []
    seen_this_run: set[tuple[str, str, str]] = set()

    for item in raw:
        name = (item.get("name") or item.get("displayName") or "").strip()
        name_lower = name.lower()
        if "spay" in name_lower or "neuter" in name_lower:
            continue

        item_type = (item.get("itemType") or {}).get("type", "").lower()
        item_type_label = (item.get("itemType") or {}).get("label", "").lower()
        if item_type not in INCLUDE_TYPES and item_type_label not in INCLUDE_TYPES:
            continue

        patient_id = (item.get("patient") or {}).get("id", "")
        patient = patients_by_id.get(patient_id)
        if not patient:
            continue
        code = patient["asm_code"]
        if code not in asm_names:
            continue

        date_str = fmt_date_for_asm(item.get("date", ""))
        dedup_key = (code, name_lower, date_str)
        if dedup_key in seen_this_run:
            continue
        seen_this_run.add(dedup_key)

        qty = item.get("quantity", "")
        uom = (item.get("itemUom") or {}).get("label", "")
        dosage = f"{qty} {uom}".strip() if qty else ""

        row = {
            "ANIMALCODE": code,
            "ANIMALNAME": asm_names[code],
            "MEDICALNAME": name,
            "MEDICALGIVENDATE": date_str,
            "MEDICALDOSAGE": dosage,
            "MEDICALCOMMENTS": f"Invoice: {(item.get('invoice') or {}).get('label', '')}",
        }

        existing = asm_existing_medical.get(code, [])
        if any(e["name"].strip().lower() == name_lower and e.get("date") == date_str for e in existing):
            skipped.append(row)
            continue

        rows.append(row)

    log.info("Medical notes rows to write: %d (skipped %d already in ASM)", len(rows), len(skipped))
    return rows, skipped


def main():
    parser = argparse.ArgumentParser(description=FLOW_NAME)
    parser.add_argument("--live", action="store_true", help="Actually write to ASM. Default is dry run.")
    args = parser.parse_args()

    log.info("=== %s started at %s (live=%s) ===", FLOW_NAME, datetime.now(timezone.utc).isoformat(), args.live)

    token = daysmart.get_token()
    patients = daysmart.get_active_patients_with_asm_code(token)

    animals = asm.get_shelter_animals()
    asm_names = asm.get_animal_names(animals)

    asm_existing_medical = load_asm_existing_medical()
    if asm_existing_medical is None:
        log.error(
            "Could not read the required ASM report '%s' -- refusing to write any "
            "medical notes data this run (writing without a working duplicate check "
            "is exactly what this script exists to prevent). See SETUP in the docstring.",
            ASM_MEDICAL_REPORT_TITLE,
        )
        send_sync_report(FLOW_NAME, [], REPORT_TO, dry_run=not args.live, send_failed=True)
        log.info("=== %s aborted at %s ===", FLOW_NAME, datetime.now(timezone.utc).isoformat())
        return

    rows, skipped = build_rows(token, patients, asm_names, asm_existing_medical)

    ok = asm.csv_import(rows, FLOW_NAME, args.live)
    if args.live:
        asm.post_sync_cleanup(dry_run=False)

    send_sync_report(
        FLOW_NAME,
        rows if ok else [],
        REPORT_TO,
        dry_run=not args.live,
        send_failed=not ok,
        skipped_duplicates=skipped,
    )

    log.info("=== %s complete at %s ===", FLOW_NAME, datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    main()
