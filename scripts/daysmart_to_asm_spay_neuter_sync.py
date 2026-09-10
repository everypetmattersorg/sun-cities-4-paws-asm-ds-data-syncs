"""
DaySmart to ASM Neuter/Spay Data Sync
------------------------------------
Marks the matching ASM animal as neutered via csv_import, from two
independent DaySmart signals:

  1. A spay/neuter procedure billed in DaySmart (/invoice-items where the
     item name contains "spay" or "neuter") -- carries a real procedure
     date.
  2. The patient's own 'sex' field already showing spayed/neutered (DaySmart
     id 2 or 4) -- covers an animal that arrived already fixed, so there was
     never a DaySmart-billed procedure to match on. No procedure date is
     available for this signal, so ANIMALNEUTEREDDATE is left blank rather
     than guessed.

Signal 1 takes priority when both fire for the same animal, since it has a
real date.

Duplicate check: an animal already marked NEUTERED=1 in ASM is skipped
entirely -- this never re-sends or overwrites an animal ASM already has
correct. Within a single run, only the first matching signal per animal is
used if more than one is found (see DaySmart to ASM precedence note above).

Matching: the ASM shelter code embedded in the DaySmart patient name (e.g.
"Biscuit - A2024001"). Deceased/adopted/inactive animals are excluded on
both sides (see common/asm.py and common/daysmart.py).

Run (dry run, default):  python3 scripts/daysmart_to_asm_spay_neuter_sync.py
Run (live):               python3 scripts/daysmart_to_asm_spay_neuter_sync.py --live
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from common import asm, daysmart
from common.matching import DS_SEX_ALTERED_IDS, fmt_date_for_asm
from common.report import send_sync_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

REPORT_TO = [addr.strip() for addr in os.environ.get("SPAY_NEUTER_SYNC_REPORT_TO", "").split(",") if addr.strip()]

FLOW_NAME = "DaySmart to ASM Neuter/Spay Data Sync"


def build_rows(
    token: str,
    patients_by_id: dict[str, dict],
    asm_names: dict[str, str],
    asm_neutered: dict[str, object],
) -> tuple[list[dict], list[dict]]:
    """Returns (rows_to_write, skipped_already_neutered_in_asm)."""
    raw = daysmart.paginate(token, "invoice-items")
    rows, skipped = [], []
    seen_this_run: set[str] = set()

    def add(code: str, date_value: str) -> None:
        if code not in asm_names or code in seen_this_run:
            return
        seen_this_run.add(code)
        row = {
            "ANIMALCODE": code,
            "ANIMALNAME": asm_names[code],
            "ANIMALNEUTERED": "Y",
            "ANIMALNEUTEREDDATE": fmt_date_for_asm(date_value) if date_value else "",
        }
        if asm_neutered.get(code):
            skipped.append(row)
        else:
            rows.append(row)

    # Signal 1: a billed "spay"/"neuter" invoice item -- has a real procedure date.
    for item in raw:
        name = (item.get("name") or item.get("displayName") or "").lower()
        if "spay" not in name and "neuter" not in name:
            continue
        patient_id = (item.get("patient") or {}).get("id", "")
        patient = patients_by_id.get(patient_id)
        if not patient:
            continue
        add(patient["asm_code"], item.get("date", ""))

    # Signal 2: the patient's own 'sex' field already shows altered -- covers
    # an animal that arrived already fixed, with no billed procedure to match
    # signal 1 on. Only fires for a code signal 1 didn't already claim.
    for patient in patients_by_id.values():
        sex_id = (patient.get("sex") or {}).get("id")
        if sex_id in DS_SEX_ALTERED_IDS:
            add(patient["asm_code"], "")

    log.info("Spay/neuter rows to write: %d (skipped %d already marked neutered in ASM)", len(rows), len(skipped))
    return rows, skipped


def main():
    parser = argparse.ArgumentParser(description=FLOW_NAME)
    parser.add_argument("--live", action="store_true", help="Actually write to ASM. Default is dry run.")
    args = parser.parse_args()

    log.info("=== %s started at %s (live=%s) ===", FLOW_NAME, datetime.now(timezone.utc).isoformat(), args.live)

    token = daysmart.get_token()
    patients = daysmart.get_active_patients_with_asm_code(token)
    patients_by_id = {p["id"]: p for p in patients}

    animals = asm.get_shelter_animals()
    asm_names = asm.get_animal_names(animals)
    asm_neutered = asm.get_animal_field(animals, "NEUTERED")

    rows, skipped = build_rows(token, patients_by_id, asm_names, asm_neutered)

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
