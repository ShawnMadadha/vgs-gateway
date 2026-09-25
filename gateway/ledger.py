"""Append-only ledger. Every attempt to move money is a row, whether it worked or not."""
import csv
import io
import time
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from gateway.vendors import SUCCEEDED, VENDORS

ROWS: list[dict] = []


def fee_for(vendor: str, amount: int) -> int:
    v = VENDORS[vendor]
    return int((Decimal(amount) * v.fee_pct).quantize(Decimal("1"), ROUND_HALF_UP)) + v.fee_fixed


def record(kind: str, payment_id: str, line_item: str, vendor: str, outcome: str, amount: int, currency: str,
           vendor_ref: str | None = None, reason: str = "", region: str = "") -> dict:
    fee = fee_for(vendor, amount) if kind == "charge" and outcome == SUCCEEDED else 0  # fees only on successful charges
    row = {"ts": time.time(), "kind": kind, "payment_id": payment_id, "line_item": line_item, "vendor": vendor,
           "region": region, "outcome": outcome, "amount": amount, "currency": currency, "fee": fee,
           "vendor_ref": vendor_ref or "", "reason": reason}
    ROWS.append(row)
    return row


def summary() -> list[dict]:
    """Gross, refunds, fees, net per vendor and currency. All integers in minor units, so no float drift."""
    totals: dict[tuple[str, str], dict] = defaultdict(lambda: {"gross": 0, "refunds": 0, "fees": 0, "charges": 0})
    for r in ROWS:
        if r["outcome"] != SUCCEEDED:
            continue
        t = totals[(r["vendor"], r["currency"])]
        if r["kind"] == "charge":
            t["gross"] += r["amount"]
            t["fees"] += r["fee"]
            t["charges"] += 1
        elif r["kind"] == "refund":
            t["refunds"] += r["amount"]
    return [{"vendor": v, "currency": c, **t, "net": t["gross"] - t["refunds"] - t["fees"]}
            for (v, c), t in sorted(totals.items())]


def vendor_stats() -> list[dict]:
    """Charge attempts vs successes per vendor and region. This is what Atlas wants for the APAC comparison."""
    stats: dict[tuple[str, str], dict] = defaultdict(lambda: {"attempts": 0, "succeeded": 0})
    for r in ROWS:
        if r["kind"] != "charge":
            continue
        s = stats[(r["vendor"], r["region"])]
        s["attempts"] += 1
        s["succeeded"] += r["outcome"] == SUCCEEDED
    return [{"vendor": v, "region": g, **s, "success_rate": round(s["succeeded"] / s["attempts"], 3)}
            for (v, g), s in sorted(stats.items())]


def to_csv() -> str:
    out = io.StringIO()
    if ROWS:
        w = csv.DictWriter(out, fieldnames=list(ROWS[0].keys()))
        w.writeheader()
        w.writerows(ROWS)
    return out.getvalue()
