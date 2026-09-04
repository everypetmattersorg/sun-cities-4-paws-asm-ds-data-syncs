"""
DaySmart to ASM Patient Profile Data Sync
------------------------------------------
Gap-fills date of birth, color, and breed from DaySmart patients onto their
matching ASM shelter animal record, via csv_import.

Duplicate check: a field is only sent when ASM's current value is blank, OR
(DOB only) more than DOB_TOLERANCE_DAYS off from DaySmart's value -- an
animal whose ASM record already has a matching value for a field is skipped
for that field, so a re-run never re-sends unchanged data. If BOTH sides
have a populated but genuinely different value, nothing is written -- that
is a conflict for a human to resolve, not something this script guesses at.

WEIGHT IS NOT SYNCED. DaySmart's patient object has no weight field on
either the /patients list or a direct single-patient GET (confirmed via
--inspect during the field-mapping review), and /patients/{id}/visits --
the likely place a per-exam weight would live -- returns an auth-scheme
error rather than data. Out of scope until DaySmart's real weight endpoint
is found.

FIELD NAMES REQUIRING VERIFICATION: the ASM csv_import column names for
DOB/color/breed below (ASM_CSV_COLUMN_*) are best-effort guesses following
the ANIMALxxx convention already proven for microchip (ANIMALMICROCHIP) --
NOT yet confirmed against ASM's csv_import source/docs. Run once via
workflow_dispatch in dry-run and check the log for csv_import "errors" on
these columns before enabling --live.

Matching: the ASM shelter code embedded in the DaySmart patient name (e.g.
"Biscuit - A2024001"). Deceased/adopted/inactive animals are excluded on
both sides (see common/asm.py and common/daysmart.py).

Run (dry run, default):  python3 scripts/daysmart_to_asm_patient_profile_sync.py
Run (live):               python3 scripts/daysmart_to_asm_patient_profile_sync.py --live
"""

from __future__ import annotations

import argparse
import logging
import os
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

REPORT_TO = [addr.strip() for addr in os.environ.get("PATIENT_PROFILE_SYNC_REPORT_TO", "").split(",") if addr.strip()]

FLOW_NAME = "DaySmart to ASM Patient Profile Data Sync"

ASM_CSV_COLUMN_DOB = "ANIMALDOB"          # UNVERIFIED -- see docstring
ASM_CSV_COLUMN_COLOR = "ANIMALCOLOUR"     # UNVERIFIED -- see docstring
ASM_CSV_COLUMN_BREED = "ANIMALBREED"      # UNVERIFIED -- see docstring

DOB_TOLERANCE_DAYS = 3


def build_rows(
    patients: list[dict],
    asm_names: dict[str, str],
    asm_dob: dict[str, str],
    asm_color: dict[str, str],
    asm_breed: dict[str, str],
) -> tuple[list[dict], list[dict]]:
    """Returns (rows_to_write, skipped_duplicates). One row per animal with
    ANIMALCODE/ANIMALNAME plus only the fields that actually need writing."""
    rows, skipped = [], []

    for p in patients:
        code = p["asm_code"]
        if code not in asm_names:
            continue

        ds_dob = p.get("birthdate") or ""
        ds_color = (p.get("color") or "").strip()
        ds_breeds = p.get("breeds") or []
        ds_breed = (ds_breeds[0].get("label") or "").strip() if ds_breeds else ""

        fields_to_write: dict[str, str] = {}
        fields_skipped: dict[str, str] = {}

        if ds_dob:
            current_dob = asm_dob.get(code, "")
            if not current_dob or not dates_close(current_dob, ds_dob, DOB_TOLERANCE_DAYS):
                if not current_dob:
                    fields_to_write[ASM_CSV_COLUMN_DOB] = fmt_date_for_asm(ds_dob)
                # else: both populated and genuinely different -- conflict, skip silently (reported elsewhere)
            else:
                fields_skipped[ASM_CSV_COLUMN_DOB] = fmt_date_for_asm(ds_dob)

        if ds_color:
            current_color = asm_color.get(code, "")
            if not current_color:
                fields_to_write[ASM_CSV_COLUMN_COLOR] = ds_color
            elif current_color.strip().lower() == ds_color.lower():
                fields_skipped[ASM_CSV_COLUMN_COLOR] = ds_color

        if ds_breed:
            current_breed = asm_breed.get(code, "")
            if not current_breed:
                fields_to_write[ASM_CSV_COLUMN_BREED] = ds_breed
            elif current_breed.strip().lower() == ds_breed.lower():
                fields_skipped[ASM_CSV_COLUMN_BREED] = ds_breed

        if fields_to_write:
            rows.append({"ANIMALCODE": code, "ANIMALNAME": asm_names[code], **fields_to_write})
        if fields_skipped:
            skipped.append({"ANIMALCODE": code, "ANIMALNAME": asm_names[code], **fields_skipped})

    log.info("Patient profile rows to write: %d (skipped %d already up to date)", len(rows), len(skipped))
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
    asm_dob = asm.get_animal_field(animals, "DATEOFBIRTH")
    asm_color = asm.get_animal_field(animals, "BASECOLOURNAME")
    asm_breed = asm.get_animal_field(animals, "BREEDNAME")

    rows, skipped = build_rows(patients, asm_names, asm_dob, asm_color, asm_breed)

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
