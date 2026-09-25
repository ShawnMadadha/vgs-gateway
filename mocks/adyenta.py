"""Adyenta sandbox mock. SOAP 1.1 XML over HTTP, per docs/ADYENTA.md. Run on port 4002."""
import asyncio
import random
import secrets
import xml.etree.ElementTree as ET  # element building only
from defusedxml.ElementTree import fromstring  # safe parser, blocks entity expansion attacks
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

from fastapi import FastAPI, Header, Request
from fastapi.responses import Response

app = FastAPI(title="Adyenta mock")

USERNAME, PASSWORD = "AtlasVoyages_TEST", "sandbox-secret"
NS = {"soap": "http://schemas.xmlsoap.org/soap/envelope/", "pay": "http://payment.adyenta.com/v12"}
TOKENS: dict[str, str] = {}        # token -> card number
TXNS: dict[str, dict] = {}         # pspReference -> {result, value, currency, refunded}
OUTAGE = {"on": False}             # sandbox-only switch to simulate the vendor being down

TEST_CARDS = {
    "4000000000009995": ("refuse", "51", "Not enough balance"),
    "4000000000009979": ("refuse", "43", "Stolen card"),
    "4000000000000119": ("fault", None, None),
    "4000000000005900": ("hang", None, None),
}

ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:pay="http://payment.adyenta.com/v12"><soap:Body>{body}</soap:Body></soap:Envelope>'
)


def ok(body: str) -> Response:
    return Response(ENVELOPE.format(body=body), media_type="text/xml")


def fault(code: str, message: str) -> Response:
    body = f"<soap:Fault><faultcode>soap:Server</faultcode><faultstring>[{code}] {message}</faultstring></soap:Fault>"
    return Response(ENVELOPE.format(body=body), media_type="text/xml", status_code=500)


def text(node: ET.Element | None, path: str) -> str:
    found = node.find(path, NS) if node is not None else None
    return (found.text or "").strip() if found is not None else ""


@app.post("/__sandbox/outage")
async def toggle_outage(on: bool):
    OUTAGE["on"] = on
    return OUTAGE


@app.post("/soap/Payment/v12")
async def soap(req: Request, soapaction: str = Header(default="")):
    await asyncio.sleep(random.uniform(0.3, 0.8))  # sandbox adds 300 to 800ms to every call
    if OUTAGE["on"]:
        return fault("905", "Internal error — transaction may not have been processed")
    try:
        root = fromstring(await req.body())
    except ET.ParseError:
        return fault("702", "Malformed envelope")
    header, body = root.find("soap:Header", NS), root.find("soap:Body", NS)
    if text(header, "pay:Security/pay:Username") != USERNAME or text(header, "pay:Security/pay:Password") != PASSWORD:
        return fault("010", "Authentication failure")
    op = soapaction.strip('"')

    if op == "CreateCardToken":
        card = body.find("pay:CreateCardTokenRequest/pay:card", NS)
        number = text(card, "pay:number")
        if not (number.isdigit() and 13 <= len(number) <= 19 and text(card, "pay:cvc")):
            return fault("702", "Invalid card details")
        tok = f"ADYC-{secrets.randbelow(10000):04d}-{secrets.randbelow(10000):04d}-{secrets.randbelow(10000):04d}"
        TOKENS[tok] = number
        return ok(f"<pay:CreateCardTokenResponse><pay:cardToken>{tok}</pay:cardToken></pay:CreateCardTokenResponse>")

    if op == "AuthoriseAndCapture":
        r = body.find("pay:AuthoriseAndCaptureRequest", NS)
        value, currency, token = text(r, "pay:amount/pay:value"), text(r, "pay:amount/pay:currency"), text(r, "pay:cardToken")
        try:
            amount = Decimal(value)
        except InvalidOperation:
            return fault("702", "Malformed amount")
        if amount <= 0 or currency not in ("USD", "EUR") or token not in TOKENS or not text(r, "pay:merchantReference"):
            return fault("702", "Request validation failure")
        behavior, code, reason = TEST_CARDS.get(TOKENS[token], ("ok", None, None))
        if behavior == "hang":
            await asyncio.sleep(30)
            behavior = "fault"
        if behavior == "fault":
            return fault("905", "Internal error — transaction may not have been processed")
        psp = str(secrets.randbelow(10**16)).zfill(16)  # every accepted request is a new transaction, no idempotency
        if behavior == "refuse":
            TXNS[psp] = {"result": "REFUSED", "value": amount, "currency": currency, "refunded": Decimal(0)}
            return ok(f"<pay:AuthoriseAndCaptureResponse><pay:pspReference>{psp}</pay:pspReference>"
                      f"<pay:resultCode>REFUSED</pay:resultCode><pay:refusalCode>{code}</pay:refusalCode>"
                      f"<pay:refusalReason>{reason}</pay:refusalReason></pay:AuthoriseAndCaptureResponse>")
        TXNS[psp] = {"result": "AUTHORISED", "value": amount, "currency": currency, "refunded": Decimal(0)}
        stamp = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S CET")  # vendor's odd date format
        return ok(f"<pay:AuthoriseAndCaptureResponse><pay:pspReference>{psp}</pay:pspReference>"
                  f"<pay:resultCode>AUTHORISED</pay:resultCode><pay:processedAt>{stamp}</pay:processedAt>"
                  f"</pay:AuthoriseAndCaptureResponse>")

    if op == "RefundTransaction":
        r = body.find("pay:RefundTransactionRequest", NS)
        txn = TXNS.get(text(r, "pay:originalPspReference"))
        try:
            amount = Decimal(text(r, "pay:amount/pay:value"))
        except InvalidOperation:
            return fault("702", "Malformed amount")
        if txn is None or txn["result"] != "AUTHORISED" or amount <= 0 or amount > txn["value"] - txn["refunded"]:
            return fault("702", "Refund validation failure")
        txn["refunded"] += amount
        if txn["refunded"] == txn["value"]:
            txn["result"] = "REFUNDED"
        psp = str(secrets.randbelow(10**16)).zfill(16)
        TXNS[psp] = {"result": "REFUNDED", "value": amount, "currency": txn["currency"], "refunded": amount}
        return ok(f"<pay:RefundTransactionResponse><pay:pspReference>{psp}</pay:pspReference>"
                  f"<pay:resultCode>REFUNDED</pay:resultCode></pay:RefundTransactionResponse>")

    if op == "GetTransactionStatus":
        psp = text(body, "pay:GetTransactionStatusRequest/pay:pspReference")
        if psp not in TXNS:
            return fault("702", "Unknown pspReference")
        return ok(f"<pay:GetTransactionStatusResponse><pay:pspReference>{psp}</pay:pspReference>"
                  f"<pay:resultCode>{TXNS[psp]['result']}</pay:resultCode></pay:GetTransactionStatusResponse>")

    return fault("702", f"Unknown operation {op!r}")
