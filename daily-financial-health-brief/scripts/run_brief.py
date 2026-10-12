#!/usr/bin/env python3
"""Daily Financial Health Brief: fetch, validate, normalize, report.

Reads the three view-only Google Sheets fresh on every run, identifies each
source's role from its field meaning, validates every row, writes normalized
CSVs and a draft report for human review. Read-only: it never writes to a
source system and never performs a financial action.

Runtime: Python 3.10+, standard library only.

Usage (from the skill root, daily-financial-health-brief/):
    python scripts/run_brief.py \
        --source <sheet URL> --source <sheet URL> --source <sheet URL> \
        --reporting-date 2026-08-11 [--prior-business-date 2026-08-10] \
        [--attested-ledger-version ledger-2026-08-11-v2] \
        [--out ../deliverables]
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

# ---------------------------------------------------------------------------
# Source contracts. A role is recognised by the fields it carries, never by
# filename, tab name or column position.
# ---------------------------------------------------------------------------

ROLES = {
    "transactions": [
        "transaction_id", "date", "account", "category", "description", "amount",
        "currency", "status", "source", "source_version", "amount_status",
    ],
    "budget": [
        "period", "category", "budget_amount", "currency", "owner", "review_rule",
        "source", "source_version",
    ],
    "revenue": ["date", "source", "metric", "value", "currency", "source_version"],
}

STATUSES = {"posted", "pending", "disputed"}
AMOUNT_STATUSES = {"confirmed", "unknown"}
CURRENCY = "USD"
MONEY_METRICS = {"collected_revenue", "outstanding_balance", "payment_plan_balance"}

# Materiality: an overage is material only when strictly above BOTH thresholds.
MATERIAL_PCT = Decimal("0.10")
MATERIAL_ABS = Decimal("500")

CENT = Decimal("0.01")


class SourceError(Exception):
    """A source could not be fetched or failed validation."""


@dataclass
class Source:
    url: str
    spreadsheet_id: str
    gid: str | None
    title: str = ""
    tab: str = ""
    fetched_at: str = ""
    header: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)
    role: str = ""

    @property
    def versions(self) -> list[str]:
        seen: list[str] = []
        for r in self.rows:
            v = r.get("source_version", "").strip()
            if v and v not in seen:
                seen.append(v)
        return seen


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

SHEET_RE = re.compile(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)")


def parse_sheet_url(url: str) -> tuple[str, str | None]:
    m = SHEET_RE.search(url)
    if not m:
        raise SourceError(f"not a Google Sheets URL: {url}")
    parsed = urllib.parse.urlparse(url)
    gid = None
    for part in (parsed.fragment, parsed.query):
        q = urllib.parse.parse_qs(part)
        if "gid" in q:
            gid = q["gid"][0]
    return m.group(1), gid


def fetch(url: str) -> Source:
    sheet_id, gid = parse_sheet_url(url)
    export = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    if gid is not None:
        export += f"&gid={gid}"
    src = Source(url=url, spreadsheet_id=sheet_id, gid=gid)
    req = urllib.request.Request(export, headers={"User-Agent": "daily-financial-health-brief"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            ctype = resp.headers.get("Content-Type", "")
            disp = resp.headers.get("Content-Disposition", "")
            body = resp.read()
    except (urllib.error.URLError, TimeoutError) as e:
        raise SourceError(f"fetch failed for {url}: {e}") from e
    src.fetched_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if "text/csv" not in ctype:
        # A sign-in page or error page instead of data: view-only access failed.
        raise SourceError(f"{url} did not return CSV (Content-Type {ctype!r}); check view-only access")
    src.title, src.tab = _names_from_disposition(disp)
    text = body.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    table = [row for row in reader]
    if not table:
        raise SourceError(f"{url} returned an empty sheet")
    src.header = [h.strip() for h in table[0]]
    for i, row in enumerate(table[1:], start=2):
        if not any(c.strip() for c in row):
            continue  # blank spacer row, not a record
        if len(row) > len(src.header):
            raise SourceError(f"{src.tab or url} row {i}: more cells than header columns")
        row = row + [""] * (len(src.header) - len(row))
        rec = dict(zip(src.header, (c.strip() for c in row)))
        rec["_row"] = str(i)
        src.rows.append(rec)
    return src


def _names_from_disposition(disp: str) -> tuple[str, str]:
    """Spreadsheet title and tab name from Google's export filename: '<title> - <tab>.csv'."""
    m = re.search(r"filename\*=UTF-8''([^;]+)", disp)
    name = urllib.parse.unquote(m.group(1)) if m else ""
    if not name:
        m = re.search(r'filename="([^"]+)"', disp)
        name = m.group(1) if m else ""
    name = re.sub(r"\.csv$", "", name)
    if " - " in name:
        title, tab = name.rsplit(" - ", 1)
        return title, tab
    return name, ""


def assign_role(src: Source) -> str:
    cols = set(src.header)
    matches = [role for role, req in ROLES.items() if set(req) <= cols]
    if len(matches) != 1:
        raise SourceError(
            f"{src.url}: cannot identify source role from fields {src.header} "
            f"(matched {matches or 'none'})"
        )
    return matches[0]


# ---------------------------------------------------------------------------
# Validation and normalization
# ---------------------------------------------------------------------------


def money(raw: str, where: str) -> Decimal:
    try:
        return Decimal(raw.replace(",", "")).quantize(CENT)
    except InvalidOperation as e:
        raise SourceError(f"{where}: not a number: {raw!r}") from e


def iso_date(raw: str, where: str) -> dt.date:
    try:
        return dt.date.fromisoformat(raw)
    except ValueError as e:
        raise SourceError(f"{where}: not an ISO date: {raw!r}") from e


def fmt(d: Decimal | None) -> str:
    return "" if d is None else f"{d:.2f}"


def signed(d: Decimal) -> str:
    return f"{'+' if d >= 0 else '-'}{abs(d):,.2f} {CURRENCY}"


def check_duplicates(rows, key, label, issues):
    """Identical duplicates are flagged; conflicting duplicates stop the run."""
    seen: dict[tuple, dict] = {}
    for r in rows:
        k = key(r)
        if k in seen:
            a = {c: v for c, v in seen[k].items() if c != "_row"}
            b = {c: v for c, v in r.items() if c != "_row"}
            if a != b:
                raise SourceError(f"{label}: conflicting duplicate identity {k} (rows {seen[k]['_row']}, {r['_row']})")
            r["_duplicate"] = "1"
            issues.append(f"{label} identity {k} appears twice with identical values (rows {seen[k]['_row']}, {r['_row']}); counted once — confirm with source owner.")
        else:
            seen[k] = r


def validate_transactions(src: Source, issues: list[str]) -> list[dict]:
    out = []
    for r in src.rows:
        where = f"transactions row {r['_row']} ({r.get('transaction_id') or 'no id'})"
        if not r["transaction_id"]:
            raise SourceError(f"{where}: missing transaction_id")
        d = iso_date(r["date"], where)
        status = r["status"].lower()
        if status not in STATUSES:
            raise SourceError(f"{where}: unrecognised status {r['status']!r}")
        astat = r["amount_status"].lower()
        if astat not in AMOUNT_STATUSES:
            raise SourceError(f"{where}: unrecognised amount_status {r['amount_status']!r}")
        if r["currency"] != CURRENCY:
            raise SourceError(f"{where}: currency {r['currency']!r}, expected {CURRENCY}")
        if astat == "confirmed":
            if not r["amount"]:
                raise SourceError(f"{where}: amount_status confirmed but amount is blank")
            amount = money(r["amount"], where)
        else:
            if r["amount"]:
                raise SourceError(f"{where}: amount_status unknown but an amount is present; ask the ledger owner which is right")
            amount = None
        if status == "posted" and amount is None:
            raise SourceError(f"{where}: posted row with unknown amount")
        out.append({**r, "_date": d, "_amount": amount, "status": status, "amount_status": astat})
    check_duplicates(out, lambda r: (r["transaction_id"],), "transactions", issues)
    return out


def validate_budget(src: Source, issues: list[str]) -> list[dict]:
    out = []
    for r in src.rows:
        where = f"budget row {r['_row']} ({r.get('period')}/{r.get('category')})"
        if not re.fullmatch(r"\d{4}-\d{2}", r["period"]):
            raise SourceError(f"{where}: period must be YYYY-MM")
        if not r["category"]:
            raise SourceError(f"{where}: missing category")
        if r["currency"] != CURRENCY:
            raise SourceError(f"{where}: currency {r['currency']!r}, expected {CURRENCY}")
        if not r["budget_amount"]:
            # Blank is not zero: a zero is a known baseline, blank is unknown.
            amount = None
            issues.append(f"Budget target for {r['period']}/{r['category']} is blank (unknown, not zero); variance not assessed — ask Finance.")
        else:
            amount = money(r["budget_amount"], where)
        out.append({**r, "_amount": amount})
    check_duplicates(out, lambda r: (r["period"], r["category"]), "budget", issues)
    return out


def validate_revenue(src: Source, issues: list[str]) -> list[dict]:
    out = []
    for r in src.rows:
        where = f"revenue row {r['_row']} ({r.get('date')}/{r.get('metric')})"
        d = iso_date(r["date"], where)
        if not r["metric"]:
            raise SourceError(f"{where}: missing metric")
        if r["metric"] in MONEY_METRICS and r["currency"] != CURRENCY:
            raise SourceError(f"{where}: monetary metric with currency {r['currency']!r}")
        value = money(r["value"], where) if r["value"] else None
        if value is None:
            issues.append(f"Revenue {r['date']}/{r['metric']} has no value; left blank — ask the tuition-system owner.")
        out.append({**r, "_date": d, "_value": value})
    # The source column describes business origin; identity is (date, metric).
    check_duplicates(out, lambda r: (r["date"], r["metric"]), "revenue", issues)
    return out


def write_csv(path: Path, columns: list[str], rows: list[dict], overrides) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(columns)
        for r in rows:
            vals = overrides(r)
            w.writerow([vals.get(c, r.get(c, "")) for c in columns])


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def counted(rows):
    return [r for r in rows if not r.get("_duplicate")]


def total(rows, day: dt.date, status: str) -> Decimal:
    return sum(
        (r["_amount"] for r in counted(rows)
         if r["_date"] == day and r["status"] == status and r["_amount"] is not None),
        Decimal("0.00"),
    )


def budget_comparison(tx, budget, reporting: dt.date, issues):
    period = reporting.strftime("%Y-%m")
    month_start = reporting.replace(day=1)
    targets = {b["category"]: b for b in counted(budget) if b["period"] == period}
    if not targets:
        raise SourceError(f"budget has no targets for period {period}")
    mtd = [r for r in counted(tx) if month_start <= r["_date"] <= reporting and r["status"] == "posted"]
    actual: dict[str, Decimal] = {}
    for r in mtd:
        actual[r["category"]] = actual.get(r["category"], Decimal("0.00")) + r["_amount"]

    lines = []
    for cat, b in targets.items():
        base = b["_amount"]
        spent = actual.get(cat)
        if spent is None:
            lines.append((cat, base, None, None, "no observed activity", b))
            continue
        if base is None:
            lines.append((cat, None, spent, None, "baseline unknown — ask Finance", b))
            continue
        var = spent - base
        material = var > MATERIAL_ABS and var > base * MATERIAL_PCT
        status = "MATERIAL OVERAGE" if material else ("over budget, not material" if var > 0 else "within budget")
        lines.append((cat, base, spent, var, status, b))
    unmapped = sorted(set(actual) - set(targets))
    for cat in unmapped:
        lines.append((cat, None, actual[cat], None, "UNMAPPED — escalate to Finance", None))
        issues.append(f"Category '{cat}' has posted spend {signed(actual[cat])} month to date but no {period} budget target; escalate the missing mapping to Finance.")
    return period, lines


def revenue_comparison(rev, reporting: dt.date, prior: dt.date, issues):
    by = {(r["_date"], r["metric"]): r for r in counted(rev)}
    metrics = []
    for r in counted(rev):
        if r["metric"] not in metrics:
            metrics.append(r["metric"])
    out = []
    for m in metrics:
        cur, prev = by.get((reporting, m)), by.get((prior, m))
        if cur is None or prev is None:
            missing = reporting if cur is None else prior
            issues.append(f"Revenue metric '{m}' has no observation for {missing.isoformat()}; change not computed.")
        out.append((m, cur, prev))
    return out


def unresolved_queue(tx, reporting: dt.date):
    start = reporting.replace(day=1)
    return [r for r in tx if start <= r["_date"] <= reporting and r["status"] in ("pending", "disputed")]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def build_report(sources, tx, budget, rev, reporting, prior, attested, issues) -> str:
    posted_today = total(tx, reporting, "posted")
    pending_today = total(tx, reporting, "pending")
    disputed_today = total(tx, reporting, "disputed")
    posted_prior = total(tx, prior, "posted")
    change = posted_today - posted_prior

    def ids(day, status):
        return ", ".join(r["transaction_id"] for r in counted(tx)
                         if r["_date"] == day and r["status"] == status and r["_amount"] is not None) or "none"

    unknown_today = [r for r in counted(tx) if r["_date"] == reporting and r["_amount"] is None]
    period, blines = budget_comparison(tx, budget, reporting, issues)
    rlines = revenue_comparison(rev, reporting, prior, issues)
    queue = sorted(unresolved_queue(tx, reporting), key=lambda r: (r["_date"], r["transaction_id"]))

    ledger = sources["transactions"]
    ledger_versions = ledger.versions
    if attested:
        if ledger_versions == [attested]:
            coverage = (f"Ledger version `{attested}` matches the source owner's completeness statement "
                        f"for {reporting.replace(day=1).isoformat()} to {reporting.isoformat()}; this run read it completely.")
        else:
            coverage = (f"Ledger version(s) {ledger_versions} differ from the attested `{attested}`; "
                        "month-to-date completeness is NOT verified for this read.")
            issues.append(coverage + " Ask Operations to confirm coverage before relying on month-to-date figures.")
    else:
        coverage = "No completeness statement supplied for this ledger version; month-to-date coverage is unverified."
        issues.append(coverage)

    L = []
    a = L.append
    a(f"# Daily Financial Health Brief — {reporting.isoformat()}")
    a("")
    a("**DRAFT FOR HUMAN REVIEW.** Prepared read-only from the sources below. It contains no payment "
      "instructions and changed no source system. The Operations owner decides every unresolved item.")
    a("")
    a(f"- Reporting date: **{reporting.isoformat()}**")
    a(f"- Prior business day: **{prior.isoformat()}**")
    a(f"- Budget period: **{period}** (month to date {reporting.replace(day=1).isoformat()} to {reporting.isoformat()}, posted activity only)")
    a("")
    a("## Headline figures")
    a("")
    a("Daily totals use only ledger rows dated that day. Posted, pending and disputed are kept separate. "
      "Pending and disputed totals include confirmed amounts only; rows with an unknown amount stay outside "
      "every total and appear in the unresolved queue.")
    a("")
    a("| Figure | Amount | Rows |")
    a("|---|---:|---|")
    a(f"| Reporting-date posted total ({reporting.isoformat()}) | {signed(posted_today)} | {ids(reporting, 'posted')} |")
    a(f"| Reporting-date pending-confirmed total ({reporting.isoformat()}) | {signed(pending_today)} | {ids(reporting, 'pending')} |")
    a(f"| Reporting-date disputed-confirmed total ({reporting.isoformat()}) | {signed(disputed_today)} | {ids(reporting, 'disputed')} |")
    a(f"| Prior-business-day posted total ({prior.isoformat()}) | {signed(posted_prior)} | {ids(prior, 'posted')} |")
    a(f"| Change in posted total ({reporting.isoformat()} minus {prior.isoformat()}) | {signed(change)} | derived |")
    a("")
    if unknown_today:
        a("Excluded from reporting-date totals because the amount is unknown: "
          + "; ".join(f"{r['transaction_id']} ({r['status']}, {r['category']}, {r['description']})" for r in unknown_today) + ".")
        a("")

    a("## Budget comparison (month to date, posted only)")
    a("")
    a(f"An overage is material only when it is strictly above both {int(MATERIAL_PCT * 100)}% of the budget "
      f"and {signed(MATERIAL_ABS)}. A zero budget is a known baseline, so only the {signed(MATERIAL_ABS)} test can bite.")
    a("")
    a("| Category | Owner | Budget | MTD posted | Variance | Assessment |")
    a("|---|---|---:|---:|---:|---|")
    for cat, base, spent, var, status, b in blines:
        a(f"| {cat} | {b['owner'] if b else '—'} | {signed(base) if base is not None else '—'} | "
          f"{signed(spent) if spent is not None else '—'} | {signed(var) if var is not None else '—'} | {status} |")
    a("")
    risks = [x for x in blines if x[4].startswith(("MATERIAL", "UNMAPPED"))]
    if risks:
        a("**Budget risks:** " + "; ".join(f"{c} ({s.lower()})" for c, _, _, _, s, _ in risks) + ".")
    else:
        a("**Budget risks:** no material overages or unmapped categories month to date.")
    a("")

    a("## Revenue and outstanding balances")
    a("")
    a("Each metric is a dated observation from the revenue snapshot. Outstanding balance and payment plan "
      "balance are reported as separate metrics; their relationship has not been confirmed by Finance.")
    a("")
    a(f"| Metric | {prior.isoformat()} | {reporting.isoformat()} | Change | Source versions |")
    a("|---|---:|---:|---:|---|")
    for m, cur, prev in rlines:
        money_m = m in MONEY_METRICS

        def show(r):
            if r is None or r["_value"] is None:
                return "—"
            return signed(r["_value"]) if money_m else f"{r['_value'].normalize():f}"

        if cur and prev and cur["_value"] is not None and prev["_value"] is not None:
            diff = cur["_value"] - prev["_value"]
            chg = signed(diff) if money_m else f"{'+' if diff >= 0 else '-'}{abs(diff).normalize():f}"
        else:
            chg = "—"
        vers = " / ".join(r["source_version"] for r in (prev, cur) if r)
        a(f"| {m} | {show(prev)} | {show(cur)} | {chg} | {vers} |")
    a("")

    a("## Unresolved queue")
    a("")
    a(f"Every pending or disputed ledger row dated {reporting.replace(day=1).isoformat()} to {reporting.isoformat()}. "
      "Nothing here is settled; each needs a decision by the Operations owner (refunds also follow Finance's "
      "`review_all_pending_or_disputed` rule).")
    a("")
    a("| Transaction | Date | Status | Category | Description | Amount | Amount status |")
    a("|---|---|---|---|---|---:|---|")
    for r in queue:
        amt = signed(r["_amount"]) if r["_amount"] is not None else "unknown (blank)"
        a(f"| {r['transaction_id']} | {r['date']} | {r['status']} | {r['category']} | {r['description']} | {amt} | {r['amount_status']} |")
    a("")
    flagged = [r for r in queue if r["status"] == "disputed" and r["category"] == "refunds"]
    if flagged:
        a("**Flag for the Operations owner — disputed refunds still unresolved, decision needed:** "
          + "; ".join(f"{r['transaction_id']} {r['description']} {signed(r['_amount']) if r['_amount'] is not None else 'amount unknown'} "
                      f"({r['date']})" for r in flagged)
          + ". This brief takes no action on them.")
        a("")

    a("## Other items needing clarification")
    a("")
    if issues:
        for i in dict.fromkeys(issues):
            a(f"- {i}")
    else:
        a("- None found in this run.")
    a("")

    a("## Sources")
    a("")
    a(coverage)
    a("")
    a("| Role | Spreadsheet | Tab | URL | Fetched (UTC) | Source version(s) | Rows |")
    a("|---|---|---|---|---|---|---:|")
    for role in ("transactions", "budget", "revenue"):
        s = sources[role]
        a(f"| {role} | {s.title} (`{s.spreadsheet_id}`) | {s.tab or 'first tab'}{f' (gid {s.gid})' if s.gid else ''} | "
          f"{s.url} | {s.fetched_at} | {', '.join(s.versions)} | {len(s.rows)} |")
    a("")
    a("Normalized copies of every source row: `deliverables/normalized/transactions.csv`, "
      "`budget.csv`, `revenue.csv`.")
    a("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Publishing (fail safe)
# ---------------------------------------------------------------------------

OUTPUTS = ["report.md", "normalized/transactions.csv", "normalized/budget.csv", "normalized/revenue.csv"]
FAILED_MARKER = "RUN_FAILED.md"


def invalidate(out: Path, reason: str) -> None:
    """Remove the previous run's deliverables so they cannot be read as current."""
    for rel in OUTPUTS:
        p = out / rel
        if p.exists():
            p.unlink()
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (out / FAILED_MARKER).write_text(
        f"# Run failed — no current brief\n\n{stamp}\n\n{reason}\n\n"
        "Previous deliverables were removed. Fix the source problem and rerun.\n",
        encoding="utf-8",
    )


def publish(stage: Path, out: Path) -> None:
    (out / "normalized").mkdir(parents=True, exist_ok=True)
    for rel in OUTPUTS:
        shutil.move(str(stage / rel), str(out / rel))  # same-volume replace of each file
    marker = out / FAILED_MARKER
    if marker.exists():
        marker.unlink()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append", required=True, help="view-only Google Sheets URL (give all three)")
    ap.add_argument("--reporting-date", required=True, type=dt.date.fromisoformat)
    ap.add_argument("--prior-business-date", type=dt.date.fromisoformat,
                    help="defaults to the previous weekday")
    ap.add_argument("--attested-ledger-version",
                    help="ledger version covered by the source owner's completeness statement")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "deliverables"))
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    out = Path(args.out).resolve()
    reporting = args.reporting_date
    prior = args.prior_business_date or _previous_weekday(reporting)

    try:
        if len(args.source) != 3:
            raise SourceError(f"expected 3 source URLs, got {len(args.source)}")
        if prior >= reporting:
            raise SourceError("prior business date must be before the reporting date")
        sources: dict[str, Source] = {}
        for url in args.source:
            s = fetch(url)
            s.role = assign_role(s)
            if s.role in sources:
                raise SourceError(f"two sources look like '{s.role}': {sources[s.role].url} and {url}")
            sources[s.role] = s
            print(f"[source] role={s.role} spreadsheet={s.spreadsheet_id} title={s.title!r} "
                  f"tab={s.tab or 'first tab'!r} gid={s.gid or 'default'} fetched_at={s.fetched_at} "
                  f"source_version={','.join(s.versions)} rows={len(s.rows)} url={url}")

        issues: list[str] = []
        tx = validate_transactions(sources["transactions"], issues)
        budget = validate_budget(sources["budget"], issues)
        rev = validate_revenue(sources["revenue"], issues)
        if not any(r["_date"] == reporting for r in tx):
            raise SourceError(f"ledger has no rows dated the reporting date {reporting}")

        with tempfile.TemporaryDirectory(dir=out.parent if out.parent.exists() else None) as tmp:
            stage = Path(tmp)
            (stage / "normalized").mkdir()
            write_csv(stage / "normalized/transactions.csv", ROLES["transactions"], tx,
                      lambda r: {"amount": fmt(r["_amount"]), "status": r["status"], "amount_status": r["amount_status"]})
            write_csv(stage / "normalized/budget.csv", ROLES["budget"], budget,
                      lambda r: {"budget_amount": fmt(r["_amount"])})
            write_csv(stage / "normalized/revenue.csv", ROLES["revenue"], rev,
                      lambda r: {"value": fmt(r["_value"]) if r["metric"] in MONEY_METRICS else
                                 ("" if r["_value"] is None else f"{r['_value'].normalize():f}")})
            report = build_report(sources, tx, budget, rev, reporting, prior, args.attested_ledger_version, issues)
            (stage / "report.md").write_text(report, encoding="utf-8")
            publish(stage, out)
    except Exception as e:  # any failure must leave no stale deliverables behind
        invalidate(out, f"{type(e).__name__}: {e}")
        print(f"[failed] {e}", file=sys.stderr)
        print(f"[failed] previous deliverables removed; see {out / FAILED_MARKER}", file=sys.stderr)
        return 1

    print(f"[done] wrote {', '.join(str(out / o) for o in OUTPUTS)}")
    return 0


def _previous_weekday(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


if __name__ == "__main__":
    sys.exit(main())
