# Sun Cities 4 Paws / Every Pet Matters -- ASM <-> DaySmart Data Syncs

Rebuilt from scratch (September 2026) as separate, single-purpose GitHub
Actions instead of one monolithic script. Every flow below matches records
between the two systems using the ASM shelter code embedded in the
DaySmart patient name, e.g. `Biscuit - A2024001`.

## Flows

| Workflow | Direction | What it syncs | Duplicate check |
|---|---|---|---|
| `daysmart-to-asm-microchip-sync.yml` | DS -> ASM | Microchip number | Skips if ASM's chip already matches |
| `daysmart-to-asm-spay-neuter-sync.yml` | DS -> ASM | Spay/neuter status + date | Skips animals already marked neutered in ASM |
| `daysmart-to-asm-patient-profile-sync.yml` | DS -> ASM | Date of birth, color, breed | Skips fields ASM already has a matching value for |
| `daysmart-to-asm-vaccination-sync.yml` | DS -> ASM | Given vaccinations | Reads ASM's existing vaccination records live each run; **refuses to write anything if that check can't be read** |
| `daysmart-to-asm-medical-notes-sync.yml` | DS -> ASM | Medications, labs, dewormers, treatments, supplements, preventatives | Reads ASM's existing medical regimen records live each run; **refuses to write anything if that check can't be read** |
| `asm-to-daysmart-create-patients.yml` | ASM -> DS | Creates new DaySmart patients for ASM animals that don't have one yet, named `Name - ASMCODE`, populated with species/sex/breed/color/chip/DOB | Skips any ASM animal already matched in DaySmart by name or code |

All six run on the same schedule: **9:00 AM and 5:00 PM Arizona time**
(Arizona doesn't observe DST, so that's a fixed `16:00`/`00:00` UTC --
see the cron lines in each workflow file). Each can also be run manually
via `workflow_dispatch` from the Actions tab.

**Weight is not synced anywhere in this repo.** DaySmart's patient object
has no weight field on `/patients` or `/patients/{id}`, and
`/patients/{id}/visits` (the likely place a per-exam weight would live)
returns an authorization-scheme error rather than data. Out of scope until
that endpoint/auth is figured out.

## Safety: dry run by default, everywhere

Every flow defaults to **dry run** -- it logs exactly what it would write
(or create) without calling ASM's `csv_import` or DaySmart's create-patient
endpoint. Nothing goes live until you explicitly turn it on:

- **Scheduled and default `workflow_dispatch` runs** stay dry run until you
  set the repository variable `LIVE_MODE` to `true`
  (Settings -> Secrets and variables -> Actions -> Variables tab). This is
  a single switch for all six flows -- flip it only after you've reviewed a
  few dry-run report emails and they look right.
- A `workflow_dispatch` run can also force one **one-off live run** via its
  `live` checkbox, independent of `LIVE_MODE` -- useful for testing a
  single flow before flipping the switch for everyone.

## Setup

### 1. Repository secrets

Settings -> Secrets and variables -> Actions -> New repository secret:

```
ASM_BASE_URL          (defaults to https://us01d.sheltermanager.com if unset)
ASM_ACCOUNT
ASM_USERNAME
ASM_PASSWORD
DS_CLIENT_ID
DS_CLIENT_SECRET
DS_API_KEY
DS_DOMAIN
DS_SHELTER_CLIENT_ID  (see step 3)
RESEND_API_KEY
FROM_EMAIL
MICROCHIP_SYNC_REPORT_TO
SPAY_NEUTER_SYNC_REPORT_TO
PATIENT_PROFILE_SYNC_REPORT_TO
VACCINATION_SYNC_REPORT_TO
MEDICAL_NOTES_SYNC_REPORT_TO
CREATE_PATIENTS_REPORT_TO
```

`*_REPORT_TO` secrets are comma-separated email addresses; each flow emails
its own report, so different flows can go to different people if useful.

### 2. Three custom SQL reports in ASM (required for vaccination + medical notes sync)

Reports -> Add report -> SQL/Advanced type, no criteria, these **exact**
titles (the script matches on title):

**`Vaccinations (All Time)`** -- existing vaccination records, for the
duplicate check:
```sql
SELECT
    a.ShelterCode AS ShelterCode,
    a.AnimalName AS AnimalName,
    vt.VaccinationType AS VaccinationType,
    av.DateOfVaccination AS DateGiven,
    av.DateRequired AS DateRequired,
    av.DateExpires AS DateExpires,
    av.Comments AS Comments
FROM animalvaccination av
INNER JOIN animal a ON a.ID = av.AnimalID
LEFT OUTER JOIN vaccinationtype vt ON vt.ID = av.VaccinationID
WHERE av.DateOfVaccination Is Not Null
ORDER BY a.ShelterCode
```

**`Vaccination Types (All)`** -- ASM's real vaccination type names, so the
sync never has to guess a type name (an earlier version of this project's
tooling defaulted to the wrong type when a name didn't match, mislabeling
~100 records -- this report is what prevents a repeat):
```sql
SELECT ID, VaccinationType FROM vaccinationtype ORDER BY VaccinationType
```

If either is missing or misnamed, `daysmart-to-asm-vaccination-sync`
refuses to write anything that run and reports why, rather than guessing.

**`Medical Notes (All Time)`** -- existing medical/treatment records,
for the medical notes sync's duplicate check:
```sql
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
```

If missing or misnamed, `daysmart-to-asm-medical-notes-sync` refuses to
write anything that run and reports why.

### 3. Find your DaySmart shelter client ID

Run `asm-to-daysmart-create-patients` manually from the Actions tab with
"List DaySmart clients and exit" checked. Find the shelter's own client in
the log output, then set `DS_SHELTER_CLIENT_ID` (step 1) to its ID.

### 4. Field names -- verified against ASM3's real source

Every ASM `csv_import` column name used across all six flows was checked
against ASM3's actual open-source importer
(`src/asm3/csvimport.py` in [sheltermanager/asm3](https://github.com/sheltermanager/asm3)
on GitHub) before this rebuild shipped, rather than left as guesses. That
check caught two real bugs before they ever ran live:

- The vaccination sync was sending `VACCINATIONDATE`; the real column is
  **`VACCINATIONGIVENDATE`**.
- The medical notes sync was sending `MEDICALDATE`; the real column is
  **`MEDICALGIVENDATE`**.
- The patient profile sync's color and breed columns were also corrected:
  **`ANIMALCOLOR`** (not `ANIMALCOLOUR`) and **`ANIMALBREED1`** (not
  `ANIMALBREED`, the singular form originally guessed).

The same check also confirmed *why* the original vaccination mislabeling
bug happened: an unmatched `VACCINATIONTYPE` string doesn't get left
blank -- ASM's importer falls back to a shelter-configured "default
vaccination type" (apparently set to a combo vaccine on this account).
`daysmart-to-asm-vaccination-sync`'s type-matching step exists specifically
so this script never sends an unmatched type and never triggers that
fallback. Color and breed mismatches, by contrast, are confirmed to just
leave the field unset -- a data gap, not data corruption -- so those two
fields don't need the same type-matching machinery.

## Not yet built

Two pieces from the original spec are intentionally not in this repo yet:

- **A 2-hourly automated duplicate-detection-and-deletion job** across
  vaccinations/microchips/patients, with an after-the-fact email report.
  This is a deliberate departure from every other flow in this project,
  which always hands a human a reviewable report or SQL file rather than
  deleting data automatically -- it needs an explicit scope/safety-guard
  discussion (what exactly counts as a duplicate, what's un-deletable,
  rate limits, a dry-run period) before being built, not a silent
  implementation.
- Real-time or same-day duplicate cleanup beyond what each sync's own
  skip-if-matching check already does.

## Repo layout

```
scripts/
  common/
    asm.py          -- ASM3 API client (auth, csv_import, custom reports)
    daysmart.py      -- DaySmart Vetter API client (auth, pagination)
    matching.py       -- ASM-code extraction/validation, date helpers
    report.py          -- shared email report builder (Resend)
  daysmart_to_asm_microchip_sync.py
  daysmart_to_asm_spay_neuter_sync.py
  daysmart_to_asm_patient_profile_sync.py
  daysmart_to_asm_vaccination_sync.py
  daysmart_to_asm_medical_notes_sync.py
  asm_to_daysmart_create_patients.py
.github/workflows/   -- one workflow per script above
```
