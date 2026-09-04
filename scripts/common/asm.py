"""
Shared ASM3 (Animal Shelter Manager) API client used by every sync script
in this repo.

Credentials are read from the environment (see .env.example / repo secrets):
  ASM_BASE_URL, ASM_ACCOUNT, ASM_USERNAME, ASM_PASSWORD
"""

from __future__ import annotations

import base64
import csv
import io
import logging
import os

import requests

from .matching import ASM_CODE_VALID

log = logging.getLogger(__name__)

ASM_BASE_URL = os.environ.get("ASM_BASE_URL", "https://us01d.sheltermanager.com").strip()
ASM_ACCOUNT = os.environ["ASM_ACCOUNT"].strip()
ASM_USERNAME = os.environ["ASM_USERNAME"].strip()
ASM_PASSWORD = os.environ["ASM_PASSWORD"].strip()


def get_shelter_animals() -> list[dict]:
    """
    Fetch all current shelter animals (on-shelter + foster). ASM3's own
    get_shelter_animals() filters WHERE Archived=0 server-side, so deceased/
    adopted/reclaimed/transferred animals are already excluded -- see
    matching.excluded_reason() for the defensive second check every script
    still applies.
    """
    resp = requests.get(
        f"{ASM_BASE_URL}/service",
        params={
            "method": "json_shelter_animals",
            "account": ASM_ACCOUNT,
            "username": ASM_USERNAME,
            "password": ASM_PASSWORD,
        },
        timeout=30,
    )
    resp.raise_for_status()
    animals = resp.json()
    log.info("ASM: %d shelter animals fetched.", len(animals))
    return animals


def get_animal_names(animals: list[dict]) -> dict[str, str]:
    """SHELTERCODE -> ANIMALNAME, for building csv_import rows."""
    return {a["SHELTERCODE"]: a["ANIMALNAME"] for a in animals if a.get("SHELTERCODE")}


def get_animal_field(animals: list[dict], field: str) -> dict[str, str]:
    """SHELTERCODE -> current value of `field`, for skip-if-already-matching checks."""
    return {
        a["SHELTERCODE"]: (a.get(field) or "").strip() if isinstance(a.get(field), str) else a.get(field)
        for a in animals if a.get("SHELTERCODE")
    }


def _ci_get(row: dict, *names: str):
    """Case-insensitive dict lookup -- ASM's SQL report response may not
    preserve the exact casing of the SELECT aliases depending on the
    underlying DB backend."""
    upper = {str(k).upper(): v for k, v in row.items()}
    for name in names:
        if name.upper() in upper:
            return upper[name.upper()]
    return None


def get_report(title: str) -> list[dict] | None:
    """
    Read a custom SQL report created in ASM (Reports -> Add report, SQL/
    Advanced type, no criteria) via json_report. Returns None (not []) if
    the call failed -- e.g. the title doesn't match what's saved in ASM, or
    the report hasn't been created yet -- so callers can distinguish "no
    rows" from "couldn't read the report at all" and report the latter
    honestly instead of treating everything as a gap.
    """
    try:
        resp = requests.get(
            f"{ASM_BASE_URL}/service",
            params={
                "method": "json_report",
                "title": title,
                "account": ASM_ACCOUNT,
                "username": ASM_USERNAME,
                "password": ASM_PASSWORD,
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, list):
            raise ValueError(f"unexpected response shape: {type(raw)}")
        return raw
    except Exception as e:
        log.warning(
            "Could not read ASM report '%s' (%s). Confirm the report exists "
            "in ASM with this exact title and no required criteria.",
            title, e,
        )
        return None


def login() -> requests.Session:
    session = requests.Session()
    resp = session.post(
        f"{ASM_BASE_URL}/login",
        data={"username": ASM_USERNAME, "password": ASM_PASSWORD, "database": ASM_ACCOUNT},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp.raise_for_status()
    if "WRONGSERVER" in resp.text or "LOGIN" in resp.text.upper()[:200]:
        raise RuntimeError(f"ASM login failed: {resp.text[:200]}")
    log.info("ASM session authenticated.")
    return session


def csv_import(rows: list[dict], description: str, live: bool) -> bool:
    """
    Send rows to ASM via csv_import. Every row must have an ANIMALCODE key;
    rows with an invalid/missing code are dropped before sending as a last-
    resort safety check (codes should already be validated by the caller).

    Returns True if the batch was sent successfully (or there was nothing
    to send), False if the ASM import call itself failed.
    """
    if not rows:
        log.info("No rows to send for: %s", description)
        return True

    safe_rows = [r for r in rows if ASM_CODE_VALID.match(r.get("ANIMALCODE", ""))]
    if len(safe_rows) < len(rows):
        log.warning(
            "%d row(s) blocked -- invalid/missing ANIMALCODE -- for: %s",
            len(rows) - len(safe_rows), description,
        )
    if not safe_rows:
        log.info("No safe rows remain to send for: %s", description)
        return True

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(safe_rows[0].keys()))
    writer.writeheader()
    writer.writerows(safe_rows)
    csv_data = output.getvalue()

    if not live:
        log.info("[DRY RUN] Would send %d row(s) to ASM for: %s", len(safe_rows), description)
        log.info("[DRY RUN] CSV preview:\n%s", csv_data)
        return True

    encoded = base64.b64encode(csv_data.encode("utf-8")).decode("utf-8")
    log.info("Sending %d row(s) to ASM for: %s", len(safe_rows), description)
    # POST, not GET -- a GET query string can exceed the server's URL length
    # limit once a batch gets into the hundreds of rows.
    resp = requests.post(
        f"{ASM_BASE_URL}/service",
        data={
            "method": "csv_import",
            "account": ASM_ACCOUNT,
            "username": ASM_USERNAME,
            "password": ASM_PASSWORD,
            "data": encoded,
            "encoding": "utf-8",
        },
        timeout=60,
    )
    if resp.status_code == 200:
        result = resp.json()
        errors = result.get("errors", [])
        log.info(
            "  ASM import result for %s -- rows: %d, success: %d, errors: %d",
            description, result.get("rows", 0), result.get("success", 0), len(errors),
        )
        for err in errors:
            log.warning("  Row error: %s", err)
        return True
    log.error("  ASM import failed for %s: %s -- %s", description, resp.status_code, resp.text[:300])
    return False


def delete_animal(session: requests.Session, animal_id) -> bool:
    r = session.post(
        f"{ASM_BASE_URL}/animal",
        data={"mode": "delete", "animalid": animal_id},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    return r.status_code == 200 and r.text.strip() in ("", "None", "null")


def post_sync_cleanup(dry_run: bool) -> None:
    """
    After a csv_import run, fetch all shelter animals and delete any that
    have a DS-prefixed shelter code -- these are phantom records created as
    a side effect of csv_import processing a row for an animal no longer in
    ASM (confirmed behavior from earlier in this project). Every sync
    script that writes to ASM calls this after its csv_import calls.
    """
    animals = get_shelter_animals()
    ds_animals = [a for a in animals if (a.get("SHELTERCODE") or "").upper().startswith("DS")]

    if not ds_animals:
        log.info("Post-sync cleanup: no DS phantom records found. ASM is clean.")
        return

    log.warning(
        "Post-sync cleanup: found %d DS phantom record(s) to remove: %s",
        len(ds_animals),
        [f"{a['SHELTERCODE']} / {a['ANIMALNAME']} (ID {a['ID']})" for a in ds_animals],
    )

    if dry_run:
        log.info("[DRY RUN] Would delete %d DS phantom record(s).", len(ds_animals))
        return

    session = login()
    deleted = 0
    for a in ds_animals:
        if delete_animal(session, a["ID"]):
            log.info("  Deleted DS phantom: %s / %s (ID %s)", a["SHELTERCODE"], a["ANIMALNAME"], a["ID"])
            deleted += 1
        else:
            log.warning("  Failed to delete %s (ID %s)", a["SHELTERCODE"], a["ID"])
    log.info("Post-sync cleanup: deleted %d / %d DS phantom record(s).", deleted, len(ds_animals))
