"""
ASM to DaySmart Patient Creation
------------------------------------
Finds ASM shelter animals (on-shelter + foster) that have no matching
DaySmart patient yet, and creates a new DaySmart patient for each, named
"NAME - ASMCODE" (e.g. ASM animal "Biscuit" / A2024001 -> DaySmart patient
"Biscuit - A2024001"), populated with whatever profile info ASM already has
at creation time: species, sex (from SEXNAME + NEUTERED), breed, color,
microchip, and date of birth.

VACCINATIONS ARE NOT POPULATED AT CREATION: there is no confirmed DaySmart
API for creating a reminder/vaccination record (only /reminders GET has
been confirmed this project) -- an animal's DaySmart vaccination history
still has to come in through daysmart_to_asm_vaccination_sync.py's
counterpart direction once it's been recorded in DaySmart directly, or
entered manually. WEIGHT IS NOT POPULATED for the same reason weight isn't
synced anywhere else in this repo -- no confirmed DaySmart field.

Duplicate check: an ASM animal is skipped if DaySmart already has a patient
matching it by full "Name - CODE", by base name alone (not yet tagged with
an ASM code), or by the ASM code appearing anywhere in any patient's name.
A base-name-only match (same name, no ASM tag yet) is a NAME COLLISION,
not an automatic match -- this script never guesses whether it's the same
animal or a coincidence, and never creates a second profile for it. These
are surfaced in the "Needs manual review" section of the email report
every run, so staff catch new ones without anyone having to dig through
logs: if it's the same animal, rename the existing DaySmart patient to
"Name - ASMCODE" and the next run recognizes it automatically; if it's a
different animal, no action needed.

SETUP:
  1. Add DS_SHELTER_CLIENT_ID to the repo's secrets/.env.
     Run with --list-clients first to find the right ID.
  2. All other credentials are shared with the other sync scripts.

Run (find shelter client):  python3 scripts/asm_to_daysmart_create_patients.py --list-clients
Run (dry run, default):      python3 scripts/asm_to_daysmart_create_patients.py
Run (live):                  python3 scripts/asm_to_daysmart_create_patients.py --live
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(__file__))

from common import asm, daysmart
from common.matching import ASM_CODE_VALID, DS_SEX_LABEL, excluded_reason, map_sex_from_asm, normalise
from common.report import send_sync_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

DS_SHELTER_CLIENT_ID = os.environ.get("DS_SHELTER_CLIENT_ID", "").strip()
REPORT_TO = [addr.strip() for addr in os.environ.get("CREATE_PATIENTS_REPORT_TO", "").split(",") if addr.strip()]

FLOW_NAME = "ASM to DaySmart Patient Creation"

ASM_CODE_PATTERN = re.compile(r"\s*-\s*([A-Z]\d{4,})\s*$")
ASM_CODE_ANYWHERE = re.compile(r"\b([A-Z]\d{4,})\b")  # finds ASM codes anywhere in a name

_SPECIES_SYNONYMS: dict[str, list[str]] = {
    "cat": ["cat", "feline", "kitten"],
    "dog": ["dog", "canine", "puppy"],
    "rabbit": ["rabbit", "bunny"],
    "bird": ["bird", "avian", "parrot", "cockatiel"],
}


# ---------------------------------------------------------------------------
# DaySmart scan + create
# ---------------------------------------------------------------------------

def ds_scan_clients(token: str) -> tuple[dict, dict, dict, set[str], set[str], set[str], dict]:
    """
    Collects reference data from DaySmart:
      species_map, sex_map, breed_map -- normalised label -> DaySmart id(s)
      asm_codes_in_ds  -- set of ASM codes found in ANY DaySmart patient name
      ds_base_names    -- normalised patient base names (ASM code suffix stripped)
      ds_full_names    -- normalised full patient names
      clients_summary  -- client id -> {name, patient_count}
    """
    species_map: dict[str, dict] = {}
    sex_map: dict[str, dict] = {}
    breed_map: dict[str, int] = {}
    asm_codes_in_ds: set[str] = set()
    ds_base_names: set[str] = set()
    ds_full_names: set[str] = set()
    clients_summary: dict = {}

    for client in daysmart.paginate(token, "clients"):
        cid = client["id"]
        fname = client.get("firstName", "")
        lname = client.get("lastName", "")
        cname = (fname + " " + lname).strip() or client.get("companyName", "") or cid
        clients_summary[cid] = {"name": cname, "patient_count": len(client.get("patients", []))}
        for p in client.get("patients", []):
            _collect_reference_maps(p, species_map, sex_map, breed_map)

    total_patients = 0
    for p in daysmart.paginate(token, "patients"):
        total_patients += 1
        pname = p.get("name", "")
        ds_full_names.add(normalise(pname))
        base = ASM_CODE_PATTERN.sub("", pname).strip()
        ds_base_names.add(normalise(base))
        for m in ASM_CODE_ANYWHERE.finditer(pname):
            asm_codes_in_ds.add(m.group(1))
        _collect_reference_maps(p, species_map, sex_map, breed_map)

    log.info("DaySmart: %d total patients scanned for duplicates.", total_patients)
    log.info("  ASM codes already in DaySmart: %d", len(asm_codes_in_ds))
    log.info("  Species available: %s", sorted(species_map.keys()))
    log.info("  Sexes available: %s", sorted(sex_map.keys()))
    log.info("  Breeds available: %d", len(breed_map))
    return species_map, sex_map, breed_map, asm_codes_in_ds, ds_base_names, ds_full_names, clients_summary


def _collect_reference_maps(p: dict, species_map: dict, sex_map: dict, breed_map: dict) -> None:
    sp = p.get("species")
    if isinstance(sp, dict) and sp.get("id"):
        key = normalise(sp.get("label", ""))
        if key and key not in species_map:
            species_map[key] = {"id": sp["id"], "label": sp.get("label", "")}
    sx = p.get("sex")
    if isinstance(sx, dict) and sx.get("id"):
        key = normalise(sx.get("label", ""))
        if key and key not in sex_map:
            sex_map[key] = {"id": sx["id"], "label": sx.get("label", "")}
    for breed in p.get("breeds") or []:
        if isinstance(breed, dict) and breed.get("id"):
            key = normalise(breed.get("label", ""))
            if key and key not in breed_map:
                breed_map[key] = breed["id"]


def ds_create_patient(token: str, payload: dict) -> bool:
    url = f"{daysmart.DS_DOMAIN}/api/1.0.0/{daysmart.DS_API_KEY}/patients"
    resp = requests.post(
        url,
        headers={**daysmart.headers(token), "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    if resp.status_code in (200, 201):
        return True
    log.warning("  Failed to create '%s': HTTP %s -- %s", payload.get("name"), resp.status_code, resp.text[:400])
    return False


def map_species(asm_value: str, species_map: dict) -> dict | None:
    key = normalise(asm_value)
    if key in species_map:
        return species_map[key]
    for canonical, aliases in _SPECIES_SYNONYMS.items():
        if any(a in key for a in aliases):
            for ds_key, ds_val in species_map.items():
                if any(a in ds_key for a in aliases):
                    return ds_val
    return None


def map_breed(asm_value: str, breed_map: dict) -> int | None:
    key = normalise(asm_value)
    if key in breed_map:
        return breed_map[key]
    for ds_key, ds_id in breed_map.items():  # partial match, e.g. "domestic shorthair" vs "shorthair"
        if key in ds_key or ds_key in key:
            return ds_id
    for ds_key, ds_id in breed_map.items():  # "Mix"/"Mixed"/"Unknown"/"Domestic" as last resort
        if any(word in ds_key for word in ("mix", "mixed", "unknown", "domestic")):
            return ds_id
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=FLOW_NAME)
    parser.add_argument("--live", action="store_true", help="Actually create patients in DaySmart. Default is dry run.")
    parser.add_argument("--list-clients", action="store_true", help="List DaySmart clients with patient counts, then exit.")
    args = parser.parse_args()

    log.info("=== %s started at %s (live=%s) ===", FLOW_NAME, datetime.now(timezone.utc).isoformat(), args.live)

    token = daysmart.get_token()

    log.info("Scanning DaySmart clients/patients for reference data …")
    species_map, sex_map, breed_map, asm_codes_in_ds, ds_base_names, ds_full_names, clients_summary = ds_scan_clients(token)

    if args.list_clients:
        log.info("--- DaySmart clients (sorted by patient count) ---")
        for cid, info in sorted(clients_summary.items(), key=lambda x: -x[1]["patient_count"]):
            log.info("  ID: %-14s  Patients: %-4d  Name: %s", cid, info["patient_count"], info["name"])
        log.info("Set DS_SHELTER_CLIENT_ID=<id>, then re-run without --list-clients.")
        return

    shelter_client_id = DS_SHELTER_CLIENT_ID
    if not shelter_client_id:
        log.error("DS_SHELTER_CLIENT_ID is not set. Run with --list-clients to find it.")
        sys.exit(1)
    if shelter_client_id not in clients_summary:
        log.error("DS_SHELTER_CLIENT_ID='%s' was not found in DaySmart. Run --list-clients to verify.", shelter_client_id)
        sys.exit(1)
    log.info("Shelter DaySmart client: %s (%s)", shelter_client_id, clients_summary[shelter_client_id]["name"])

    log.info("Fetching ASM shelter animals …")
    asm_animals = asm.get_shelter_animals()

    to_create = []
    name_collisions: list[dict] = []
    skipped_in_ds = skipped_bad_code = skipped_deceased_archived = 0

    for animal in asm_animals:
        name = (animal.get("ANIMALNAME") or "").strip()
        code = (animal.get("SHELTERCODE") or "").strip()

        if not code or not ASM_CODE_VALID.match(code):
            skipped_bad_code += 1
            continue

        reason = excluded_reason(animal)
        if reason:
            skipped_deceased_archived += 1
            continue

        ds_name = f"{name} - {code}"
        if normalise(ds_name) in ds_full_names:
            skipped_in_ds += 1
            continue
        if normalise(name) in ds_base_names:
            # A DaySmart patient with the same base name exists but isn't
            # tagged with this ASM code yet -- never guess whether it's the
            # same animal. Surfaced in the report below for a human to
            # either rename the existing DaySmart patient to "Name - CODE"
            # (if it's the same animal -- future runs then recognize it
            # automatically) or leave alone (if it's a different animal).
            log.info("  SKIPPED '%s' -- name '%s' already exists in DaySmart (no ASM tag yet).", ds_name, name)
            name_collisions.append({
                "ASM Code": code, "ASM Name": name,
                "Would-Be DaySmart Name": ds_name, "DaySmart Name Matched": name,
            })
            skipped_in_ds += 1
            continue
        if code in asm_codes_in_ds:
            skipped_in_ds += 1
            continue

        to_create.append(animal)

    log.info(
        "ASM animals: %d total -> %d already in DaySmart, %d to create, %d bad/missing code, %d deceased/archived.",
        len(asm_animals), skipped_in_ds, len(to_create), skipped_bad_code, skipped_deceased_archived,
    )

    if not to_create:
        log.info("Nothing to do -- all ASM animals are already in DaySmart.")
        send_sync_report(
            FLOW_NAME, [], REPORT_TO, dry_run=not args.live,
            written_label="Created in DaySmart",
            skipped_duplicates=name_collisions,
            skipped_label="Needs manual review -- name collision in DaySmart (no ASM tag yet)",
            skipped_note=(
                "A DaySmart patient with this name already exists but isn't tagged with the "
                'ASM code. If it\'s the same animal, rename it to "Name - ASMCODE" in DaySmart '
                "and future runs will recognize it automatically. If it's a different animal, "
                "no action needed."
            ),
        )
        log.info("=== %s complete at %s ===", FLOW_NAME, datetime.now(timezone.utc).isoformat())
        return

    created_animals: list[dict] = []
    failed = 0
    skipped_no_species = 0

    for animal in to_create:
        name = (animal.get("ANIMALNAME") or "").strip()
        code = (animal.get("SHELTERCODE") or "").strip()
        asm_species = (animal.get("SPECIESNAME") or animal.get("ANIMALTYPENAME") or "").strip()
        asm_sexname = (animal.get("SEXNAME") or "").strip()
        asm_neutered = int(animal.get("NEUTERED") or 0)
        asm_breed = (animal.get("BREEDNAME") or animal.get("BREED1NAME") or "").strip()
        asm_color = (animal.get("BASECOLOURNAME") or "").strip()
        asm_dob = (animal.get("DATEOFBIRTH") or "").strip()
        asm_chip = (animal.get("IDENTICHIPNUMBER") or "").strip()
        asm_loc = (animal.get("SHELTERLOCATIONNAME") or animal.get("ACTIVEMOVEMENTTYPENAME") or "").strip()
        ds_name = f"{name} - {code}"

        sp = map_species(asm_species, species_map)
        if sp is None:
            log.warning("  SKIPPED '%s' -- cannot map ASM species '%s' to any DaySmart species.", ds_name, asm_species)
            skipped_no_species += 1
            continue

        sex_id = map_sex_from_asm(asm_sexname, asm_neutered)
        breed_id = map_breed(asm_breed, breed_map)

        payload: dict = {
            "name": ds_name,
            "client": {"id": shelter_client_id},
            "species": {"id": sp["id"]},
            "sex": {"id": sex_id},
            "status": "Active",
        }
        if breed_id is not None:
            payload["breeds"] = [{"id": breed_id}]
        if asm_color:
            payload["color"] = asm_color
        if asm_chip:
            payload["chip"] = asm_chip
        if asm_dob:
            try:
                dob_dt = datetime.fromisoformat(asm_dob.replace("Z", "+00:00"))
                # DaySmart's real field is "birthdate" (not "birthday"), and requires
                # strict ISO8601 WITH a timezone offset -- confirmed via a real 400
                # earlier in this project against a bare no-offset timestamp.
                payload["birthdate"] = dob_dt.strftime("%Y-%m-%dT%H:%M:%S+0000")
            except Exception:
                pass

        sex_label = DS_SEX_LABEL.get(sex_id, "Unknown")
        report_row = {"Name": name, "ASM Code": code, "Species": sp["label"], "Sex": sex_label, "Location": asm_loc}

        if not args.live:
            log.info(
                "[DRY RUN] Would create: name='%s' species='%s' sex='%s' breed='%s' color='%s' chip='%s'",
                ds_name, sp["label"], sex_label, asm_breed or "(none)", asm_color or "(none)", asm_chip or "(none)",
            )
            created_animals.append(report_row)
            continue

        log.info("Creating patient: '%s' (species=%s sex=%s)", ds_name, sp["label"], sex_label)
        if ds_create_patient(token, payload):
            created_animals.append(report_row)
        else:
            failed += 1

    send_sync_report(
        FLOW_NAME, created_animals, REPORT_TO, dry_run=not args.live,
        written_label="Created in DaySmart",
        skipped_duplicates=name_collisions,
        skipped_label="Needs manual review -- name collision in DaySmart (no ASM tag yet)",
        skipped_note=(
            "A DaySmart patient with this name already exists but isn't tagged with the "
            'ASM code. If it\'s the same animal, rename it to "Name - ASMCODE" in DaySmart '
            "and future runs will recognize it automatically. If it's a different animal, "
            "no action needed."
        ),
    )

    log.info("--- Summary ---")
    log.info("Created%s: %d", " (would create)" if not args.live else "", len(created_animals))
    log.info("Already in DaySmart (skipped): %d", skipped_in_ds)
    log.info("Skipped -- species not mappable: %d", skipped_no_species)
    log.info("Failed to create: %d", failed)
    log.info("=== %s complete at %s ===", FLOW_NAME, datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    main()
