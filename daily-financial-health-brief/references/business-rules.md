# Business rules

Established in the stakeholder interview with the Finance and Operations Manager
(`interviews/interview-r62mbg-20261012-1856.md`) and the Manager report request.

## Dates

- **Reporting date** is set by the Manager report request for the upcoming operations meeting
  (for the Aug 12, 2026 meeting: 2026-08-11).
- **Prior business day** is the comparison day named in the same request (2026-08-10). The script
  defaults to the previous weekday only if none is given.
- **Month to date** runs from the first of the reporting month through the reporting date.

## Daily totals

- Use only ledger rows **dated the reporting date** (or the prior business day for its figure).
- Keep **posted**, **pending** and **disputed** separate. Never net them together.
- A pending or disputed row is **confirmed** when it carries an actual numeric amount
  (`amount_status = confirmed`). If the amount is unavailable it is **unknown**: the amount stays
  blank, it is excluded from every monetary total, and the row stays visible in the unresolved queue.
- Signed amounts are kept as signed: credits and corrections (negative rows) reduce the posted total.

The five required figures:

| Figure | Rows used |
|---|---|
| Reporting-date posted total | status `posted`, date = reporting date |
| Reporting-date pending-confirmed total | status `pending`, amount confirmed, date = reporting date |
| Reporting-date disputed-confirmed total | status `disputed`, amount confirmed, date = reporting date |
| Prior-business-day posted total | status `posted`, date = prior business day |
| Change in posted total | reporting-date posted total minus prior-business-day posted total |

## Budget comparison

- Compare **month-to-date posted** spend by category against the Budget targets for the reporting
  month. Pending and disputed activity is not spend.
- Variance = MTD posted − budget. An overage is **material** only when it is **strictly above both**
  10% of the budget **and** USD 500. Equal to a threshold is not material.
- A **zero budget is a known baseline**, not missing data: any overage above USD 500 is material.
  A **blank** budget is unknown and is raised for Finance instead of assessed.
- A ledger category with no budget target is **unmapped**: shown in the table and escalated to Finance.
- A budget category with no posted activity is labelled **no observed activity**.
- Refunds carry the `review_all_pending_or_disputed` rule: every pending or disputed refund is
  surfaced for review regardless of size.

## Revenue

- Compare each revenue metric on the reporting date with the prior business day.
- Outstanding balance and payment plan balance are **separate dated metrics**. Do not add, subtract
  or reconcile them unless Finance clarifies their relationship.

## Unresolved queue

- Every pending or disputed row dated from the first of the month through the reporting date,
  including unknown amounts.
- The **Operations owner** reviews and decides each item. The brief never resolves one itself.
- Disputed refunds are called out explicitly (the manager asked that the USD 700 disputed refund be
  flagged for the Operations owner).
