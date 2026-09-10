"""
Shared single-section HTML/email report builder. Each sync script in this
repo owns exactly one data category, so its report is one table plus a
duplicates-skipped callout -- not the multi-section report the old repo
used when one script did everything.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
FROM_EMAIL = os.environ.get("FROM_EMAIL", "").strip()

log = logging.getLogger(__name__)


def _html_table(rows: list[dict]) -> str:
    if not rows:
        return "<p style='color:#888;font-size:13px'>None this run.</p>"
    cols = list(rows[0].keys())
    head = "".join(
        f"<th style='text-align:left;padding:4px 12px 4px 0;border-bottom:1px solid #ddd'>{c}</th>"
        for c in cols
    )
    body = "".join(
        "<tr>" + "".join(f"<td style='padding:4px 12px 4px 0;font-size:13px'>{r.get(c, '')}</td>" for c in cols) + "</tr>"
        for r in rows
    )
    return f"<table style='border-collapse:collapse;width:100%'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _text_table(rows: list[dict]) -> str:
    if not rows:
        return "  None this run.\n"
    return "\n".join("  " + ", ".join(f"{k}: {v}" for k, v in r.items()) for r in rows) + "\n"


def send_sync_report(
    flow_name: str,
    written_rows: list[dict],
    recipients: list[str],
    dry_run: bool,
    send_failed: bool = False,
    skipped_duplicates: list[dict] | None = None,
    written_label: str = "Written to ASM",
    skipped_label: str = "Skipped -- already present in ASM",
    skipped_note: str = "These matched an existing ASM record and were not re-sent.",
) -> None:
    """
    Email a report of what this run wrote (or would have, in dry run).
    `skipped_duplicates` lists rows that were NOT sent, with `skipped_label`/
    `skipped_note` explaining why -- surfaced so a human sees the dedup
    check (or, for asm_to_daysmart_create_patients.py, a name collision
    needing manual attention) doing its job, not silently dropping data.
    `written_label` names what the main table actually is -- most flows
    write to ASM, but asm_to_daysmart_create_patients.py writes to
    DaySmart, so this isn't hardcoded.
    """
    if not recipients:
        log.info("No report recipients configured -- skipping report email.")
        return
    if not RESEND_API_KEY or not FROM_EMAIL:
        log.warning("RESEND_API_KEY or FROM_EMAIL not set -- skipping report email.")
        return

    skipped_duplicates = skipped_duplicates or []
    run_label = "[DRY RUN] " if dry_run else ""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    subject = f"{run_label}{flow_name} -- {len(written_rows)} record(s) — {now}"
    if send_failed:
        subject += " -- SEND FAILED"

    warning_html = ""
    warning_text = ""
    if send_failed:
        warning_html = (
            "<p style='color:#c00;font-weight:bold;background:#fee;padding:10px;"
            f"border:1px solid #c00'>WARNING: this run FAILED. "
            "Nothing below was actually written.</p>"
        )
        warning_text = "\n*** WARNING: this run FAILED -- nothing below was written. ***\n"

    dup_html = ""
    dup_text = ""
    if skipped_duplicates:
        dup_html = f"""
        <h3 style='color:#333;margin-bottom:4px'>{skipped_label} ({len(skipped_duplicates)})</h3>
        <p style='color:#666;font-size:13px'>{skipped_note}</p>
        {_html_table(skipped_duplicates)}
        """
        dup_text = f"\n{skipped_label} ({len(skipped_duplicates)}):\n{skipped_note}\n{_text_table(skipped_duplicates)}"

    html_body = f"""
    <div style='font-family:Arial,sans-serif;max-width:820px;margin:0 auto'>
      <h2 style='color:#333'>{run_label}{flow_name}</h2>
      <p style='color:#666'>Generated: {now}</p>
      {warning_html}
      <h3 style='color:#333;margin-bottom:4px'>{written_label} ({len(written_rows)})</h3>
      {_html_table(written_rows)}
      {dup_html}
      <p style='color:#aaa;font-size:12px;margin-top:32px'>
        Every Pet Matters / Sun Cities 4 Paws -- automated sync
      </p>
    </div>
    """
    text_body = "\n".join([
        flow_name,
        f"Generated: {now}",
        warning_text,
        f"{written_label} ({len(written_rows)}):",
        _text_table(written_rows),
        dup_text,
    ])

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={"from": FROM_EMAIL, "to": recipients, "subject": subject, "text": text_body, "html": html_body},
        timeout=15,
    )
    if resp.status_code in (200, 201):
        log.info("Report emailed to %s", ", ".join(recipients))
    else:
        log.error("Failed to send report email: %s -- %s", resp.status_code, resp.text[:300])
