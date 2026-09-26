"""Routing, retry, and rollback for one package payment."""
import hashlib
import itertools

from gateway import ledger
from gateway.vendors import HARD_DECLINE, INVALID, SUCCEEDED, UNKNOWN, VENDORS, Card, Result

PRIMARY = {"US": "stripely", "EU": "adyenta"}  # today's routing, straight from the stakeholder
_apac_turn = itertools.cycle(["stripely", "adyenta"])  # APAC rotates so Atlas can compare vendors


def candidates(region: str, currency: str) -> list[str]:
    """Vendors to try in order. Primary for the region first, then anyone else who takes the currency."""
    first = PRIMARY.get(region) or next(_apac_turn)
    order = [first] + [n for n in VENDORS if n != first]
    return [n for n in order if currency in VENDORS[n].currencies]


def attempt_key(payment_id: str, item_id: str, vendor: str) -> str:
    # Deterministic per payment, item, and vendor: a crash-and-retry replays the same Stripely charge.
    return hashlib.sha256(f"{payment_id}:{item_id}:{vendor}".encode()).hexdigest()[:32]


async def process(payment: dict, card: Card) -> dict:
    """Charge every line item, retrying once at the other vendor. If any item fails, refund everything."""
    tokens: dict[str, str] = {}  # vendor -> token, so a card is tokenized once per vendor, never stored
    charged: list[dict] = []
    payment["status"], payment["failure"] = "processing", None

    for item in payment["line_items"]:  # items after a failure stay "not_attempted", which the response shows
        item.update(status="not_attempted", vendor=None, vendor_ref=None, attempts=[])
    for item in payment["line_items"]:
        vendors = candidates(item["region"], item["currency"])
        if not vendors:
            item["status"] = "failed"
            payment["failure"] = f"no vendor supports {item['currency']} for {item['id']}"
            break
        for vendor in vendors:
            v = VENDORS[vendor]
            try:
                if vendor not in tokens:
                    tokens[vendor] = await v.tokenize(card)
            except Exception as e:  # tokenize failed, so no money moved: skip this vendor, never crash
                res = Result(INVALID, reason=f"tokenize failed: {e}"[:120])
            else:
                res = await v.charge(tokens[vendor], item["amount"], item["currency"],
                                     payment["booking_reference"], attempt_key(payment["id"], item["id"], vendor))
            ledger.record("charge", payment["id"], item["id"], vendor, res.outcome, item["amount"],
                          item["currency"], res.vendor_ref, res.reason, item["region"])
            item["attempts"].append({"vendor": vendor, "outcome": res.outcome, "reason": res.reason})
            if res.outcome == SUCCEEDED:
                item.update(status="charged", vendor=vendor, vendor_ref=res.vendor_ref, refunded=0)
                charged.append(item)
                break
            if res.outcome == UNKNOWN:
                payment.setdefault("needs_reconciliation", []).append(
                    {"line_item": item["id"], "vendor": vendor, "reason": res.reason})  # money may have moved
            if res.outcome == HARD_DECLINE:
                break  # stolen card: never retry this card anywhere
        if item["status"] != "charged":
            item["status"] = "failed"
            payment["failure"] = f"{item['id']} failed at {[a['vendor'] for a in item['attempts']]}"
            break

    if payment["failure"]:
        await rollback(payment, charged)  # all or nothing: back out the charges that did succeed
        payment["status"] = "failed"
    else:
        payment["status"] = "succeeded"
    return payment


async def rollback(payment: dict, charged: list[dict]) -> None:
    for item in charged:
        res = await refund_item(payment, item, item["amount"])
        item["status"] = "rolled_back" if res.outcome == SUCCEEDED else "rollback_failed"


async def refund_item(payment: dict, item: dict, amount: int):
    res = await VENDORS[item["vendor"]].refund(item["vendor_ref"], amount, item["currency"])
    ledger.record("refund", payment["id"], item["id"], item["vendor"], res.outcome, amount, item["currency"],
                  res.vendor_ref, res.reason, item["region"])
    if res.outcome == SUCCEEDED:
        item["refunded"] = item.get("refunded", 0) + amount
    return res
