"""VGS gateway API. One connection for Atlas: payments, refunds, ledger."""
import hashlib
import secrets
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from gateway import ledger, router
from gateway.vendors import SUCCEEDED, Card

app = FastAPI(title="VGS gateway")

PAYMENTS: dict[str, dict] = {}
IDEMPOTENCY: dict[str, tuple[str, str]] = {}  # key -> (body hash, payment id)


class CardIn(BaseModel):
    number: str = Field(pattern=r"^\d{13,19}$")
    exp_month: int = Field(ge=1, le=12)
    exp_year: int = Field(ge=2026, le=2099)
    cvc: str = Field(pattern=r"^\d{3,4}$")


class LineItem(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    description: str = ""
    amount: int = Field(gt=0, description="minor units, 1050 = 10.50")
    currency: Literal["USD", "EUR", "GBP"]
    region: Literal["US", "EU", "APAC"]


class PaymentIn(BaseModel):
    booking_reference: str = Field(min_length=1, max_length=64)
    card: CardIn
    line_items: list[LineItem] = Field(min_length=1, max_length=5)


class RefundIn(BaseModel):
    payment_id: str
    line_item_id: str | None = Field(default=None, description="omit to refund every charged item in the bundle")
    amount: int | None = Field(default=None, gt=0, description="minor units; omit for full refund")


def public(payment: dict) -> dict:
    return payment  # card details are never stored on the payment, so nothing to strip


@app.post("/v1/payments")
async def create_payment(body: PaymentIn, idempotency_key: str = Header()):
    digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    if idempotency_key in IDEMPOTENCY:
        seen_digest, pid = IDEMPOTENCY[idempotency_key]
        if seen_digest != digest:
            raise HTTPException(409, "Idempotency-Key was already used with a different request body.")
        p = PAYMENTS[pid]
        return JSONResponse(public(p), status_code=201 if p["status"] == "succeeded" else 402)
    if len({i.id for i in body.line_items}) != len(body.line_items):
        raise HTTPException(422, "line_items ids must be unique.")

    payment = {
        "id": "pay_" + secrets.token_hex(8), "booking_reference": body.booking_reference,
        "card_last4": body.card.number[-4:],  # the only card detail we keep
        "line_items": [i.model_dump() for i in body.line_items],
    }
    PAYMENTS[payment["id"]] = payment
    IDEMPOTENCY[idempotency_key] = (digest, payment["id"])  # claim the key before any vendor call
    card = Card(body.card.number, body.card.exp_month, body.card.exp_year, body.card.cvc)
    await router.process(payment, card)
    return JSONResponse(public(payment), status_code=201 if payment["status"] == "succeeded" else 402)


@app.get("/v1/payments/{payment_id}")
async def get_payment(payment_id: str):
    if payment_id not in PAYMENTS:
        raise HTTPException(404, "No such payment.")
    return public(PAYMENTS[payment_id])


@app.post("/v1/refunds")
async def create_refund(body: RefundIn, idempotency_key: str = Header()):
    if idempotency_key in IDEMPOTENCY:
        return IDEMPOTENCY[idempotency_key][1]
    payment = PAYMENTS.get(body.payment_id)
    if payment is None:
        raise HTTPException(404, "No such payment.")
    if body.line_item_id is None:  # whole bundle, e.g. the trip was cancelled
        if body.amount is not None:
            raise HTTPException(422, "amount only applies to a single line item.")
        items = [i for i in payment["line_items"] if i.get("status") == "charged" and i["amount"] > i.get("refunded", 0)]
    else:
        items = [i for i in payment["line_items"] if i["id"] == body.line_item_id and i.get("status") == "charged"]
    if not items:
        raise HTTPException(409, "Nothing refundable: item not charged, already rolled back, or fully refunded.")

    results = []
    for item in items:
        remaining = item["amount"] - item.get("refunded", 0)
        amount = body.amount or remaining
        if amount > remaining:
            raise HTTPException(409, f"Refund exceeds remaining {remaining} {item['currency']}.")
        res = await router.refund_item(payment, item, amount)
        results.append({"line_item_id": item["id"], "amount": amount, "currency": item["currency"], "vendor": item["vendor"],
                        "outcome": res.outcome, "vendor_ref": res.vendor_ref, "reason": res.reason,
                        "remaining": item["amount"] - item.get("refunded", 0)})
    out = {"payment_id": payment["id"], "refunds": results,
           "failed": [r["line_item_id"] for r in results if r["outcome"] != SUCCEEDED]}  # ops team needs to see these
    IDEMPOTENCY[idempotency_key] = ("", out)
    return JSONResponse(out, status_code=200 if not out["failed"] else 502)


@app.get("/v1/refunds/failed")
async def failed_refunds():
    """The daily feed for the ops team: every refund the vendor did not confirm, with what they need to chase it."""
    return [r for r in ledger.ROWS if r["kind"] == "refund" and r["outcome"] != SUCCEEDED]


@app.get("/v1/ledger")
async def get_ledger():
    return {"summary": ledger.summary(), "vendor_stats": ledger.vendor_stats(), "rows": ledger.ROWS}


@app.get("/v1/ledger.csv")
async def get_ledger_csv():
    return PlainTextResponse(ledger.to_csv(), media_type="text/csv")  # the finance data feed


@app.get("/")
async def index():
    return FileResponse("static/index.html")
