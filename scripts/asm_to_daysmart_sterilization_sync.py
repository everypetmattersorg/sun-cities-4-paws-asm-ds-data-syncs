"""
ASM to DaySmart Sterilization Data Sync
----------------------------------------
Fills in DaySmart's 'sex' field (which combines sex + altered-status into
one of 5 values: Male intact/neutered, Female intact/spayed, Unknown) from
ASM's SEXNAME + NEUTERED, but ONLY for a DaySmart patient whose 'sex' field
is currently Unknown (id 5) -- i.e. DaySmart genuinely has no information
yet. This is the fallback direction: DaySmart is the primary source for
sterilization status (see daysmart_to_asm_spay_neuter_sync.py, which pushes
DaySmart's data into ASM and runs far more often, since the vet clinic is
where this actually gets recorded) -- this script only pulls FROM ASM when
DaySmart has nothing of its own to lose.

An ASM animal whose own SEXNAME is also blank/Unknown contributes nothing
and is skipped -- there's no data to pull from either side.

Matching: the ASM shelter code embedded in the DaySmart patient name (e.g.
"Biscuit - A2024001"). Deceased/adopted/inactive animals are excluded on
both sides (see common/asm.py and common/daysmart.py).

Run (dry run, default):  python3 scripts/asm_to_daysmart_sterilization_sync.py
Run (live):               python3 scripts/asm_to_daysmart_sterilization_sync.py --live
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from common import asm, daysmart
from common.matching import DS_SEX_LABEL, DS_SEX_UNKNOWN_ID, map_sex_from_asm
from common.report import send_sync_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

REPORT_TO = [addr.strip() for addr in os.environ.get("STERILIZATION_SYNC_REPORT_TO", "").split(",") if addr.strip()]

FLOW_NAME = "ASM to DaySmart Sterilization Data Sync"


def build_updates(
    patients: list[dict],
    asm_sexname: dict[str, str],
    asm_neutered: dict[str, object],
) -> list[dict]:
    """One entry per DaySmart patient that needs its 'sex' field filled in
    from ASM. Each entry carries the DaySmart patient id/name plus the new
    sex id/label to write."""
    updates = []
    for p in patients:
        code = p["asm_code"]
        current_sex_id = (p.get("sex") or {}).get("id")
        if current_sex_id != DS_SEX_UNKNOWN_ID:
            continue  # DaySmart already has its own sex/altered info -- don't touch it

        sexname = asm_sexname.get(code, "")
        if not sexname or sexname.strip().lower() == "unknown":
            continue  # ASM has nothing to contribute either

        new_sex_id = map_sex_from_asm(sexname, int(asm_neutered.get(code) or 0))
        if new_sex_id == DS_SEX_UNKNOWN_ID:
            continue

        updates.append({
            "patient_id": p["id"],
            "DaySmart Patient": p.get("name", ""),
            "ASM Code": code,
            "New Sex": DS_SEX_LABEL.get(new_sex_id, "Unknown"),
            "sex_id": new_sex_id,
        })

    log.info("Sterilization updates to write to DaySmart: %d", len(updates))
    return updates


def main():
    parser = argparse.ArgumentParser(description=FLOW_NAME)
    parser.add_argument("--live", action="store_true", help="Actually write to DaySmart. Default is dry run.")
    args = parser.parse_args()

    log.info("=== %s started at %s (live=%s) ===", FLOW_NAME, datetime.now(timezone.utc).isoformat(), args.live)

    token = daysmart.get_token()
    patients = daysmart.get_active_patients_with_asm_code(token)

    animals = asm.get_shelter_animals()
    asm_sexname = asm.get_animal_field(animals, "SEXNAME")
    asm_neutered = asm.get_animal_field(animals, "NEUTERED")

    updates = build_updates(patients, asm_sexname, asm_neutered)

    written, failed = [], 0
    for u in updates:
        report_row = {"DaySmart Patient": u["DaySmart Patient"], "ASM Code": u["ASM Code"], "New Sex": u["New Sex"]}
        if not args.live:
            log.info(
                "[DRY RUN] Would update '%s' (%s) -> sex='%s'",
                u["DaySmart Patient"], u["ASM Code"], u["New Sex"],
            )
            written.append(report_row)
            continue

        log.info("Updating '%s' (%s) -> sex='%s'", u["DaySmart Patient"], u["ASM Code"], u["New Sex"])
        if daysmart.update_patient(token, u["patient_id"], {"sex": {"id": u["sex_id"]}}):
            written.append(report_row)
        else:
            failed += 1

    send_sync_report(
        FLOW_NAME,
        written,
        REPORT_TO,
        dry_run=not args.live,
        send_failed=failed > 0,
        written_label="Updated in DaySmart",
    )

    log.info("--- Summary ---")
    log.info("Updated%s: %d", " (would update)" if not args.live else "", len(written))
    log.info("Failed to update: %d", failed)
    log.info("=== %s complete at %s ===", FLOW_NAME, datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    main()
