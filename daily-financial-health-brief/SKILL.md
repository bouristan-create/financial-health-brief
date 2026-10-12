---
name: daily-financial-health-brief
description: Prepare Quillhaven Academy's Daily Financial Health Brief as a draft for human review. Freshly reads the transaction ledger, budget targets and revenue snapshot Google Sheets (view-only), validates and normalizes every row, and writes a source-traceable report with posted / pending-confirmed / disputed-confirmed totals, prior-business-day comparison, month-to-date budget risks, revenue and an unresolved queue. Use when asked for the daily financial brief, daily financial health report, or operations-meeting finance summary.
---

# Daily Financial Health Brief

Produces the Finance and Operations Manager's daily brief for the operations meeting. The output is
a **draft for human review**: the skill is read-only, changes no source system, and never issues
payment instructions or takes a financial action.

## Inputs

| Input | Where it comes from |
|---|---|
| Three Google Sheets URLs (ledger, budget, revenue) | Given by the Finance and Operations Manager. Order does not matter; roles are recognised from field meaning. |
| Reporting date | The Manager report request for the upcoming meeting. |
| Prior business day | The Manager report request; defaults to the previous weekday. |
| Attested ledger version (optional) | The source owner's completeness statement for the month-to-date ledger. |

## Run

Runtime: Python 3.10 or newer, standard library only (no install step). From this folder:

```
python scripts/run_brief.py \
  --source <ledger sheet URL> --source <budget sheet URL> --source <revenue sheet URL> \
  --reporting-date 2026-08-11 --prior-business-date 2026-08-10 \
  --attested-ledger-version ledger-2026-08-11-v2
```

Outputs go to `../deliverables/` (override with `--out`):

- `report.md` — the brief
- `normalized/transactions.csv`, `normalized/budget.csv`, `normalized/revenue.csv` — every source row in the required columns

Before publishing, the script prints each source's URL, spreadsheet id and title, tab, fetch time,
source version and row count, and writes the same into the report's Sources section.

## What the operator must do

1. Check the printed source lines: three roles, expected versions, plausible row counts.
2. Read `report.md` top to bottom. Anything under **Unresolved queue** and **Other items needing
   clarification** is a question for a person, not an answer.
3. Hand the draft to the Operations owner. Disputed refunds are flagged for their decision.

If the run fails (a sheet unreachable, a column missing, an invalid or contradictory row), the script
removes the previous deliverables and writes `deliverables/RUN_FAILED.md` with the reason. Do not
substitute a downloaded or cached copy; fix access or ask the source owner, then rerun.

## Reference

- `references/business-rules.md` — how every figure is derived and why
- `references/sources.md` — source roles, identities, validation and failure behaviour
