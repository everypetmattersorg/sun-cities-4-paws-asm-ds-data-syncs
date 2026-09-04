"""
DaySmart to ASM Microchip Data Sync
------------------------------------
Copies microchip numbers from DaySmart patients to their matching ASM
shelter animal record, via csv_import.

Duplicate check: a patient's chip is only sent if ASM's current
IDENTICHIPNUMBER for that animal is blank or different -- an animal whose
ASM record already has this exact chip number is skipped, so a re-run never
re-sends unchanged data.

Matching: the ASM shelter code embedded in the DaySmart patient name (e.g.
"Biscuit - A2024001"). Deceased/adopted/inactive animals are excluded on
both sides (see common/asm.py and common/daysmart.py).

Run (dry run, default):  python3 scripts/daysmart_to_asm_microchip_sync.py
Run (live):               python3 scripts/daysmart_to_asm_microchip_sync.py --live
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from common import asm, daysmart
from common.report import send_sync_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

REPORT_TO = [addr.strip() for addr in os.environ.get("MICROCHIP_SYNC_REPORT_TO", "").split(",") if addr.strip()]

FLOW_NAME = "DaySmart to ASM Microchip Data Sync"


def build_rows(patients: list[dict], asm_names: dict[str, str], asm_chips: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Returns (rows_to_write, skipped_duplicates)."""
    rows, skipped = [], []
    for p in patients:
        chip = (p.get("chip") or "").strip()
        code = p["asm_code"]
        if not chip or code not in asm_names:
            continue
        current = asm_chips.get(code, "")
        row = {"ANIMALCODE": code, "ANIMALNAME": asm_names[code], "ANIMALMICROCHIP": chip}
        if current == chip:
            skipped.append(row)
            continue
        rows.append(row)
    log.info("Microchip rows to write: %d (skipped %d already up to date)", len(rows), len(skipped))
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
    asm_chips = asm.get_animal_field(animals, "IDENTICHIPNUMBER")

    rows, skipped = build_rows(patients, asm_names, asm_chips)

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
