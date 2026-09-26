"""Vendor adapters. Each one speaks a vendor's native format and returns the same plain result."""
import os
from dataclasses import dataclass
from decimal import Decimal

import httpx
from defusedxml.ElementTree import fromstring

# Outcomes the router cares about. Everything vendor-specific is mapped onto these five.
SUCCEEDED, SOFT_DECLINE, HARD_DECLINE, UNKNOWN, INVALID = "succeeded", "soft_decline", "hard_decline", "unknown", "invalid"

TIMEOUT = float(os.environ.get("VENDOR_TIMEOUT_S", "5"))  # fare holds last minutes, so we do not wait 30s on a hung vendor


@dataclass
class Card:
    number: str
    exp_month: int
    exp_year: int
    cvc: str


@dataclass
class Result:
    outcome: str
    vendor_ref: str | None = None  # vendor's id for the charge or refund
    reason: str = ""


class Stripely:
    name = "stripely"
    currencies = {"USD", "GBP"}
    fee_pct, fee_fixed = Decimal("0.029"), 30  # 2.9% + $0.30, fixed part in minor units

    def __init__(self) -> None:
        self.base = os.environ.get("STRIPELY_URL", "http://localhost:4001/v1")
        self.headers = {"Authorization": f"Bearer {os.environ.get('STRIPELY_KEY', 'sk_test_atlas_51xK')}"}

    async def tokenize(self, card: Card) -> str:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(f"{self.base}/tokens", headers=self.headers, json={"card": {
                "number": card.number, "exp_month": card.exp_month, "exp_year": card.exp_year, "cvc": card.cvc}})
        r.raise_for_status()
        return r.json()["id"]

    async def charge(self, token: str, amount: int, currency: str, reference: str, idem_key: str) -> Result:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.post(f"{self.base}/charges", headers={**self.headers, "Idempotency-Key": idem_key},
                                 json={"amount": amount, "currency": currency.lower(), "source": token, "reference": reference})
        except httpx.HTTPError as e:
            return Result(UNKNOWN, reason=f"no response: {type(e).__name__}")  # money may have moved
        body = r.json()
        if r.status_code == 201:
            return Result(SUCCEEDED, body["id"])
        err = body.get("error", {})
        if r.status_code == 402:
            kind = HARD_DECLINE if err.get("decline_code") == "stolen_card" else SOFT_DECLINE
            return Result(kind, reason=err.get("decline_code", "card_declined"))
        if r.status_code >= 500:
            return Result(UNKNOWN, reason=err.get("message", "api_error"))
        return Result(INVALID, reason=err.get("message", f"http {r.status_code}"))

    async def refund(self, vendor_ref: str, amount: int, currency: str) -> Result:
        try:  # currency is implied by the charge at Stripely; kept so both adapters share one signature
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.post(f"{self.base}/refunds", headers=self.headers, json={"charge": vendor_ref, "amount": amount})
        except httpx.HTTPError as e:
            return Result(UNKNOWN, reason=f"no response: {type(e).__name__}")
        if r.status_code == 201:
            return Result(SUCCEEDED, r.json()["id"])
        return Result(INVALID if r.status_code < 500 else UNKNOWN, reason=r.json().get("error", {}).get("message", ""))


class Adyenta:
    name = "adyenta"
    currencies = {"USD", "EUR"}
    fee_pct, fee_fixed = Decimal("0.018"), 12  # 1.8% + €0.12

    NS = {"soap": "http://schemas.xmlsoap.org/soap/envelope/", "pay": "http://payment.adyenta.com/v12"}

    def __init__(self) -> None:
        self.url = os.environ.get("ADYENTA_URL", "http://localhost:4002/soap/Payment/v12")
        self.user = os.environ.get("ADYENTA_USER", "AtlasVoyages_TEST")
        self.password = os.environ.get("ADYENTA_PASS", "sandbox-secret")
        self.account = os.environ.get("ADYENTA_ACCOUNT", "AtlasVoyages")

    def envelope(self, body: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:pay="http://payment.adyenta.com/v12">'
            f"<soap:Header><pay:Security><pay:Username>{self.user}</pay:Username><pay:Password>{self.password}</pay:Password>"
            f"</pay:Security></soap:Header><soap:Body>{body}</soap:Body></soap:Envelope>"
        )

    async def call(self, op: str, body: str):
        """Returns (fault_code or None, body element or None). Raises httpx errors on no response."""
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(self.url, content=self.envelope(body),
                             headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{op}"'})
        root = fromstring(r.content)
        fault = root.find("soap:Body/soap:Fault/faultstring", self.NS)
        if fault is not None:
            return fault.text[1:4], None  # faultstring looks like "[905] message"
        return None, root.find("soap:Body", self.NS)

    @staticmethod
    def major(amount: int) -> str:
        return f"{Decimal(amount) / 100:.2f}"  # 1050 -> "10.50"; USD and EUR both have 2 decimals

    async def tokenize(self, card: Card) -> str:
        code, body = await self.call("CreateCardToken", (
            f"<pay:CreateCardTokenRequest><pay:merchantAccount>{self.account}</pay:merchantAccount><pay:card>"
            f"<pay:number>{card.number}</pay:number><pay:expiryMonth>{card.exp_month:02d}</pay:expiryMonth>"
            f"<pay:expiryYear>{card.exp_year}</pay:expiryYear><pay:cvc>{card.cvc}</pay:cvc></pay:card></pay:CreateCardTokenRequest>"))
        if code:
            raise ValueError(f"Adyenta fault [{code}] on tokenize")
        return body.find("pay:CreateCardTokenResponse/pay:cardToken", self.NS).text

    async def charge(self, token: str, amount: int, currency: str, reference: str, idem_key: str) -> Result:
        # Adyenta has no idempotency, so the reference carries our attempt key to keep retries distinguishable.
        merchant_ref = f"{reference}-{idem_key[:8]}"
        try:
            code, body = await self.call("AuthoriseAndCapture", (
                f"<pay:AuthoriseAndCaptureRequest><pay:merchantAccount>{self.account}</pay:merchantAccount>"
                f"<pay:merchantReference>{merchant_ref}</pay:merchantReference><pay:amount><pay:value>{self.major(amount)}</pay:value>"
                f"<pay:currency>{currency.upper()}</pay:currency></pay:amount><pay:cardToken>{token}</pay:cardToken>"
                f"</pay:AuthoriseAndCaptureRequest>"))
        except httpx.HTTPError as e:
            return Result(UNKNOWN, reason=f"no response: {type(e).__name__}")
        if code == "905":
            return Result(UNKNOWN, reason="fault 905")
        if code:
            return Result(INVALID, reason=f"fault {code}")
        resp = body.find("pay:AuthoriseAndCaptureResponse", self.NS)
        psp = resp.find("pay:pspReference", self.NS).text
        if resp.find("pay:resultCode", self.NS).text == "AUTHORISED":
            return Result(SUCCEEDED, psp)
        refusal = resp.find("pay:refusalCode", self.NS).text
        return Result(HARD_DECLINE if refusal == "43" else SOFT_DECLINE, reason=f"refusal {refusal}")

    async def refund(self, vendor_ref: str, amount: int, currency: str) -> Result:
        try:
            code, body = await self.call("RefundTransaction", (
                f"<pay:RefundTransactionRequest><pay:merchantAccount>{self.account}</pay:merchantAccount>"
                f"<pay:originalPspReference>{vendor_ref}</pay:originalPspReference><pay:amount><pay:value>{self.major(amount)}</pay:value>"
                f"<pay:currency>{currency}</pay:currency></pay:amount></pay:RefundTransactionRequest>"))
        except httpx.HTTPError as e:
            return Result(UNKNOWN, reason=f"no response: {type(e).__name__}")
        if code:
            return Result(UNKNOWN if code == "905" else INVALID, reason=f"fault {code}")
        return Result(SUCCEEDED, body.find("pay:RefundTransactionResponse/pay:pspReference", self.NS).text)


VENDORS = {v.name: v for v in (Stripely(), Adyenta())}

