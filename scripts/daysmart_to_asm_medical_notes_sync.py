"""
DaySmart to ASM Medical Notes Data Sync
------------------------------------
Pulls billed medical items from DaySmart /invoice-items (medications, labs/
diagnostics, dewormers, treatments, supplements, preventatives -- spay/
neuter is excluded, that's daysmart_to_asm_spay_neuter_sync.py's job) and
writes them to the matching ASM shelter animal via csv_import.

DUPLICATE CHECK -- KNOWN LIMITATION: unlike the vaccination sync, ASM has
no confirmed read API or existing custom SQL report for previously-imported
medical/regimen records, so this script cannot yet verify a given DaySmart
invoice item hasn't already been sent to ASM in an earlier run. Per this
project's requirement that nothing gets uploaded without a duplicate check,
this script REFUSES to write anything live until ASM_MEDICAL_REPORT_TITLE
below is set to a real custom SQL report (Reports -> Add report, SQL/
Advanced type, no criteria) that returns existing MEDICALNAME/MEDICALDATE
rows per ShelterCode from whichever ASM table csv_import's MEDICALNAME/
MEDICALDATE/MEDICALDOSAGE/MEDICALCOMMENTS columns actually write into --
that table has not been confirmed yet. Within a single run, exact repeat
items (same animal + name + date) are still deduped against each other.

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

# Set once a real custom SQL report exists in ASM returning existing
# MEDICALNAME/MEDICALDATE rows per ShelterCode -- see DUPLICATE CHECK above.
# Left unset intentionally: this flow will not write live data until it is.
ASM_MEDICAL_REPORT_TITLE = os.environ.get("ASM_MEDICAL_REPORT_TITLE", "").strip()

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
    if not ASM_MEDICAL_REPORT_TITLE:
        return None
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
            "date": _ci_get(row, "MedicalDate"),
        })
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
            "MEDICALDATE": date_str,
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
    if asm_existing_medical is None and args.live:
        log.error(
            "ASM_MEDICAL_REPORT_TITLE is not set (or the report couldn't be read) -- "
            "refusing to write live medical notes data without a working cross-run "
            "duplicate check. See DUPLICATE CHECK in this script's docstring. "
            "Running as dry run instead."
        )
        args.live = False
    asm_existing_medical = asm_existing_medical or {}

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
