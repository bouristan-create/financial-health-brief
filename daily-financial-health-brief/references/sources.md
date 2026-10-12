# Sources

Three Google Sheets, shared view-only by the Finance and Operations Manager. Each run reads all three
fresh through their CSV export (`/export?format=csv`, plus `&gid=` if the URL names a tab). Bundled,
downloaded or cached copies are never an input.

## Roles

Roles are recognised from the fields a sheet carries, not from its filename, title, tab name or
column order. A sheet must match exactly one role.

| Role | Identity | Required fields |
|---|---|---|
| transactions (Operations ledger) | `transaction_id` | transaction_id, date, account, category, description, amount, currency, status, source, source_version, amount_status |
| budget (Finance targets) | `period` + `category` | period, category, budget_amount, currency, owner, review_rule, source, source_version |
| revenue (tuition-system snapshot) | `date` + `metric` | date, source, metric, value, currency, source_version |

The revenue `source` column describes business origin; it does not create a second observation for
the same date and metric.

## Validation (any failure stops the run)

- Sheet returns CSV (a sign-in or error page means view-only access failed).
- All required fields present; no row wider than its header.
- Dates are ISO `YYYY-MM-DD`; budget period is `YYYY-MM`.
- Amounts parse as numbers (thousands separators accepted).
- Currency is USD for monetary rows.
- Ledger `status` is posted / pending / disputed; `amount_status` is confirmed / unknown.
- `confirmed` rows have an amount; `unknown` rows have a blank amount; posted rows are never unknown.
- Duplicate identities: identical repeats are counted once and flagged; **conflicting** duplicates
  stop the run until the source owner resolves them.
- The ledger has rows for the reporting date, and the budget has targets for the reporting month.

Blank-but-legal values (a blank budget target, a missing revenue observation) do not stop the run:
they are reported under "Other items needing clarification" and never filled in.

## Completeness

Operations states that ledger version `ledger-2026-08-11-v2` is the complete population for
1–11 August 2026 (recorded rows begin on 3 August). Pass that version as
`--attested-ledger-version`; if the ledger read returns a different version, the report says
month-to-date coverage is not verified and raises it for Operations.

## Failure behaviour

Outputs are written to a staging directory and only moved into `deliverables/` after every source
has been fetched and validated and the report built. On any failure the script deletes the previous
`report.md` and normalized CSVs and writes `deliverables/RUN_FAILED.md` with the reason, so a stale
brief can never be read as current.
