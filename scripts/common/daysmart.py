"""
Shared DaySmart Vetter API client used by every sync script in this repo.

Credentials are read from the environment (see .env.example / repo secrets):
  DS_CLIENT_ID, DS_CLIENT_SECRET, DS_API_KEY, DS_DOMAIN
"""

from __future__ import annotations

import logging
import os

import requests

from .matching import ASM_CODE_VALID, extract_asm_code

log = logging.getLogger(__name__)

DS_CLIENT_ID = os.environ["DS_CLIENT_ID"].strip()
DS_CLIENT_SECRET = os.environ["DS_CLIENT_SECRET"].strip()
DS_API_KEY = os.environ["DS_API_KEY"].strip()
DS_DOMAIN = os.environ["DS_DOMAIN"].strip()


def get_token() -> str:
    resp = requests.post(
        f"{DS_DOMAIN}/oauth/access_token",
        data={
            "grant_type": "client_credentials",
            "client_id": DS_CLIENT_ID,
            "client_secret": DS_CLIENT_SECRET,
            "scope": "APIService",
        },
        headers={
            "x-api-key": DS_CLIENT_SECRET,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=15,
    )
    resp.raise_for_status()
    log.info("DaySmart token obtained.")
    return resp.json()["access_token"]


def headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "x-api-key": DS_CLIENT_SECRET,
        "Accept": "application/json",
    }


def paginate(token: str, endpoint: str, extra_params: dict | None = None) -> list[dict]:
    """Page through a DaySmart endpoint and return all resources."""
    url = f"{DS_DOMAIN}/api/1.0.0/{DS_API_KEY}/{endpoint}"
    results: list[dict] = []
    page = 1
    while True:
        params = {"page": page, "perPage": 200, **(extra_params or {})}
        resp = requests.get(url, headers=headers(token), params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()["response"]
        results.extend(body.get("resources", []))
        meta = body.get("meta", {})
        if page >= meta.get("lastPage", 1):
            break
        page += 1
    log.info("  /%s -> %d records", endpoint, len(results))
    return results


def update_patient(token: str, patient: dict, overrides: dict) -> bool:
    """
    PUT a full-object update to an existing DaySmart patient. Confirmed via
    live testing against /patients/{id}: PATCH and POST both return HTTP 403
    with a generic auth-rejection message -- neither method is configured
    for this resource at all. PUT is accepted, but as a full-object
    replace, not a partial update: a field omitted from the payload gets
    blanked out rather than left alone (confirmed via a real PUT with only
    {"sex": ...} -- DaySmart rejected it with "name/species.id/breeds/
    status are required").

    `patient` must be the patient's own current full record (e.g. from
    get_active_patients_with_asm_code() or a /patients GET) -- its fields
    are used as the base payload so nothing already set gets lost.
    `overrides` is merged on top for just the field(s) actually being
    changed, e.g. {"sex": {"id": 4}}. A round-trip test (real PUT, then
    GET + full field diff) confirmed this only changes the overridden
    field(s) plus DaySmart's own server-managed updateAt/url_direct_jump.
    """
    payload = {
        "name": patient["name"],
        "species": patient["species"],
        "color": patient.get("color"),
        "sex": patient.get("sex"),
        "birthdate": patient.get("birthdate"),
        "chip": patient.get("chip"),
        "status": patient["status"],
        "tag": patient.get("tag", ""),
        "breeds": patient.get("breeds", []),
        **overrides,
    }
    url = f"{DS_DOMAIN}/api/1.0.0/{DS_API_KEY}/patients/{patient['id']}"
    resp = requests.put(
        url,
        headers={**headers(token), "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    if resp.status_code in (200, 201, 204):
        return True
    log.warning("  Failed to update patient %s: HTTP %s -- %s", patient["id"], resp.status_code, resp.text[:400])
    return False


def get_active_patients_with_asm_code(token: str) -> list[dict]:
    """
    All Active DaySmart patients that have a valid ASM code in their name.
    Inactive (adopted/discharged) and Deceased patients are excluded --
    every sync in this repo only touches animals currently active in both
    systems.
    """
    raw = paginate(token, "patients")
    patients = []
    skipped_status = 0
    skipped_code = 0
    for p in raw:
        if (p.get("status") or "").strip() != "Active":
            skipped_status += 1
            continue
        code = extract_asm_code(p.get("name", ""))
        if not code:
            continue
        if not ASM_CODE_VALID.match(code):
            skipped_code += 1
            log.warning(
                "SKIPPED -- suspicious ASM code '%s' extracted from DaySmart patient '%s'.",
                code, p.get("name", ""),
            )
            continue
        patients.append(p | {"asm_code": code})
    log.info(
        "DaySmart: %d active patient(s) with a valid ASM code (skipped %d inactive/deceased, %d bad code).",
        len(patients), skipped_status, skipped_code,
    )
    return patients
