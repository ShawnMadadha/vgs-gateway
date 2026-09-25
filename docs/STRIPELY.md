# Stripely API Reference (Sandbox)

> Third-party processor spec #1. Your gateway must integrate against this contract.
> You implement the mock server yourself; it must honor everything below, including the
> simulated test-card behavior.
> Machine-readable contract: `stripely.openapi.yaml` (render with `docs.html`).

Stripely is a modern card processor with a JSON-over-HTTPS API.

- **Base URL (sandbox):** `https://api.stripely.test/v1` (run your mock locally, e.g. `http://localhost:4001/v1`)
- **Auth:** `Authorization: Bearer sk_test_atlas_51xK...` on every request. Missing/invalid key → `401`.
- **Supported currencies:** `usd`, `gbp` (lowercase ISO 4217)
- **Amounts:** integers in **minor units** (`1050` = $10.50)
- **Pricing:** 2.9% + $0.30 per successful charge
- **Tokens:** cards must be tokenized with Stripely first (`POST /v1/tokens`). Charges accept only Stripely tokens (`tok_st_...`) — tokens from any other provider are rejected with `400`.
- **Idempotency:** supported natively via the `Idempotency-Key` request header on charge creation. Replaying a key within 24h returns the **original response** (same charge id, same status), and never creates a second charge.

## Create a card token

`POST /v1/tokens`

```json
{
  "card": {
    "number": "4242424242424242",
    "exp_month": 8,
    "exp_year": 2029,
    "cvc": "123"
  }
}
```

**Success — `201 Created`:**
```json
{
  "id": "tok_st_8Ge2xQb4kR9mVw1c",
  "object": "token",
  "brand": "visa",
  "last4": "4242",
  "created": 1786372290
}
```

Malformed card (non-numeric number, wrong length, missing fields, expired date) → `400 invalid_request_error`. Tokenization **succeeds even for test cards that later decline** — decline/failure behavior triggers at charge time, not tokenization time. Sandbox tokens are reusable.

## Create a charge

`POST /v1/charges`

```
Authorization: Bearer sk_test_atlas_51xK...
Idempotency-Key: 8f3c1a9e-4b2d-4f6a-9c1e-7d5b2a8e3f10
Content-Type: application/json
```
```json
{
  "amount": 1050,
  "currency": "usd",
  "source": "tok_st_8Ge2xQb4kR9mVw1c",
  "reference": "BKG-448291"
}
```

**Success — `201 Created`:**
```json
{
  "id": "ch_3PxK2mQ8rT1vY7aB",
  "object": "charge",
  "status": "succeeded",
  "amount": 1050,
  "currency": "usd",
  "source": "tok_st_8Ge2xQb4kR9mVw1c",
  "reference": "BKG-448291",
  "created": 1786372331
}
```

**Card declined — `402 Payment Required`:**
```json
{
  "error": {
    "type": "card_error",
    "code": "card_declined",
    "decline_code": "insufficient_funds",
    "message": "Your card has insufficient funds."
  }
}
```
`decline_code` values you must handle: `insufficient_funds` (soft — the issuer might approve later or via another route) and `stolen_card` (hard — never retry this card anywhere).

**Bad request — `400`:** `{"error": {"type": "invalid_request_error", "message": "..."}}`
(unsupported currency, non-integer amount, missing fields, unknown or non-Stripely `source` token)

**Processor failure — `500`:** `{"error": {"type": "api_error", "message": "Something went wrong on Stripely's end."}}`
It is **unknown** whether a charge was created. (In sandbox: it was not.)

## Retrieve a charge

`GET /v1/charges/{id}` → `200` with the charge object above, or `404`.

## Create a refund

`POST /v1/refunds`
```json
{
  "charge": "ch_3PxK2mQ8rT1vY7aB",
  "amount": 1050
}
```
`amount` is optional; omitted = full refund. Refunds reference the charge — no token needed. Refunding more than the remaining un-refunded amount → `400 invalid_request_error`.

**Success — `201`:**
```json
{
  "id": "re_1QaB3cD4eF5gH6iJ",
  "object": "refund",
  "charge": "ch_3PxK2mQ8rT1vY7aB",
  "status": "succeeded",
  "amount": 1050,
  "created": 1786375944
}
```

## Sandbox test cards

Behavior is keyed off the **card number** used to create the token, and triggers when the token is **charged**:

| Test card number | Behavior at charge time |
|---|---|
| `4242 4242 4242 4242` | `201`, charge succeeds |
| `4000 0000 0000 9995` | `402`, `decline_code: insufficient_funds` |
| `4000 0000 0000 9979` | `402`, `decline_code: stolen_card` |
| `4000 0000 0000 0119` | `500 api_error` |
| `4000 0000 0000 5900` | No response for 30 seconds, then `500` |

Any other well-formed card number → treated as `4242 4242 4242 4242`.
