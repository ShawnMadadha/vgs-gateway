"""Stripely sandbox mock. JSON over HTTP, per docs/STRIPELY.md. Run on port 4001."""
import asyncio
import secrets
import time

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Stripely mock")

API_KEY = "sk_test_atlas_51xK"
TOKENS: dict[str, dict] = {}       # token id -> {card_number, brand, last4}
CHARGES: dict[str, dict] = {}      # charge id -> charge object
REFUNDED: dict[str, int] = {}      # charge id -> minor units refunded so far
IDEMPOTENCY: dict[str, tuple[int, dict]] = {}  # key -> (status, body) of the first response

# Behavior at charge time is keyed off the card number used to make the token.
TEST_CARDS = {
    "4000000000009995": ("decline", "insufficient_funds", "Your card has insufficient funds."),
    "4000000000009979": ("decline", "stolen_card", "Your card was reported stolen."),
    "4000000000000119": ("error", None, None),
    "4000000000005900": ("hang", None, None),
}


def err(status: int, type_: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": {"type": type_, "message": message, **extra}}, status_code=status)


def check_auth(authorization: str | None) -> JSONResponse | None:
    if authorization != f"Bearer {API_KEY}":
        return err(401, "authentication_error", "Invalid API key.")
    return None


@app.post("/v1/tokens")
async def create_token(req: Request, authorization: str | None = Header(default=None)):
    if bad := check_auth(authorization):
        return bad
    body = await req.json()
    card = body.get("card") or {}
    number = str(card.get("number", ""))
    if not (number.isdigit() and 13 <= len(number) <= 19 and card.get("cvc")):
        return err(400, "invalid_request_error", "Invalid card details.")
    month, year = card.get("exp_month"), card.get("exp_year")
    if not (isinstance(month, int) and isinstance(year, int) and 1 <= month <= 12):
        return err(400, "invalid_request_error", "Invalid expiry.")
    now = time.gmtime()
    if (year, month) < (now.tm_year, now.tm_mon):
        return err(400, "invalid_request_error", "Card expired.")
    tok = "tok_st_" + secrets.token_hex(8)
    TOKENS[tok] = {"number": number, "brand": "visa", "last4": number[-4:]}
    return JSONResponse(
        {"id": tok, "object": "token", "brand": "visa", "last4": number[-4:], "created": int(time.time())},
        status_code=201,
    )


@app.post("/v1/charges")
async def create_charge(
    req: Request,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
):
    if bad := check_auth(authorization):
        return bad
    # Replaying a key returns the original response and never creates a second charge.
    if idempotency_key and idempotency_key in IDEMPOTENCY:
        status, body = IDEMPOTENCY[idempotency_key]
        return JSONResponse(body, status_code=status)

    body = await req.json()
    amount, currency, source = body.get("amount"), body.get("currency"), body.get("source")
    if not (isinstance(amount, int) and amount > 0):
        return err(400, "invalid_request_error", "amount must be a positive integer in minor units.")
    if currency not in ("usd", "gbp"):
        return err(400, "invalid_request_error", f"Unsupported currency {currency!r}.")
    if not body.get("reference"):
        return err(400, "invalid_request_error", "reference is required.")
    token = TOKENS.get(source)
    if token is None:
        return err(400, "invalid_request_error", "source must be a Stripely token.")

    behavior, code, message = TEST_CARDS.get(token["number"], ("ok", None, None))
    if behavior == "hang":
        await asyncio.sleep(30)  # simulate a vendor that never answers, then fails
        behavior = "error"
    if behavior == "error":
        resp = err(500, "api_error", "Something went wrong on Stripely's end.")
    elif behavior == "decline":
        resp = err(402, "card_error", message, code="card_declined", decline_code=code)
    else:
        charge = {
            "id": "ch_" + secrets.token_hex(8), "object": "charge", "status": "succeeded",
            "amount": amount, "currency": currency, "source": source,
            "reference": body["reference"], "created": int(time.time()),
        }
        CHARGES[charge["id"]] = charge
        resp = JSONResponse(charge, status_code=201)
    if idempotency_key:
        import json
        IDEMPOTENCY[idempotency_key] = (resp.status_code, json.loads(resp.body))
    return resp


@app.get("/v1/charges/{charge_id}")
async def get_charge(charge_id: str, authorization: str | None = Header(default=None)):
    if bad := check_auth(authorization):
        return bad
    if charge_id not in CHARGES:
        return err(404, "invalid_request_error", "No such charge.")
    return CHARGES[charge_id]


@app.post("/v1/refunds")
async def create_refund(req: Request, authorization: str | None = Header(default=None)):
    if bad := check_auth(authorization):
        return bad
    body = await req.json()
    charge = CHARGES.get(body.get("charge"))
    if charge is None:
        return err(400, "invalid_request_error", "No such charge.")
    remaining = charge["amount"] - REFUNDED.get(charge["id"], 0)
    amount = body.get("amount", remaining)  # omitted amount means full refund
    if not (isinstance(amount, int) and 0 < amount <= remaining):
        return err(400, "invalid_request_error", "Refund exceeds remaining amount.")
    REFUNDED[charge["id"]] = REFUNDED.get(charge["id"], 0) + amount
    return JSONResponse(
        {"id": "re_" + secrets.token_hex(8), "object": "refund", "charge": charge["id"],
         "status": "succeeded", "amount": amount, "created": int(time.time())},
        status_code=201,
    )
