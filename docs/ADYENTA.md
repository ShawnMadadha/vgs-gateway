# Adyenta Payment Service — SOAP Integration Guide v12 (Sandbox)

> Third-party processor spec #2. Your gateway must integrate against this contract.
> You implement the mock server yourself. **You do not need a WSDL toolchain or SOAP
> library** — accepting and returning well-formed XML envelopes over HTTP is sufficient,
> on both the mock side and the client side.
> Machine-readable contract: `adyenta.wsdl`. Where it and this document disagree, this document wins.

Adyenta is an established European processor with a SOAP 1.1 API.

- **Endpoint (sandbox):** `POST https://pal.adyenta.test/soap/Payment/v12` (run your mock locally, e.g. `http://localhost:4002/soap/Payment/v12`)
- **Headers:** `Content-Type: text/xml; charset=utf-8` and `SOAPAction: "<OperationName>"`
- **Auth:** username/password in the SOAP header (see envelopes below). Bad credentials → SOAP Fault `[010]`.
- **Supported currencies:** `USD`, `EUR` (uppercase ISO 4217)
- **Amounts:** decimal strings in **major units** (`"10.50"` = €10.50)
- **Pricing:** 1.8% + €0.12 per successful charge
- **Tokens:** cards must be tokenized via `CreateCardToken` first. `AuthoriseAndCapture` accepts only Adyenta tokens (`ADYC-...`) — any other value → Fault `[702]`. Adyenta tokens are reusable in sandbox.
- **Idempotency: NOT SUPPORTED.** Every accepted `AuthoriseAndCapture` request creates a new, distinct transaction — including identical retries with the same `merchantReference`. Duplicate submission is the merchant's problem to prevent.
- Both `AUTHORISED` and `REFUSED` outcomes return **HTTP 200**. Only system errors return HTTP 500, as a SOAP Fault.
- Sandbox adds 300–800ms of latency to every call.

## Operation: `CreateCardToken`

**Request:**
```xml
<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:pay="http://payment.adyenta.com/v12">
  <soap:Header>
    <pay:Security>
      <pay:Username>AtlasVoyages_TEST</pay:Username>
      <pay:Password>sandbox-secret</pay:Password>
    </pay:Security>
  </soap:Header>
  <soap:Body>
    <pay:CreateCardTokenRequest>
      <pay:merchantAccount>AtlasVoyages</pay:merchantAccount>
      <pay:card>
        <pay:number>4242424242424242</pay:number>
        <pay:expiryMonth>08</pay:expiryMonth>
        <pay:expiryYear>2029</pay:expiryYear>
        <pay:cvc>123</pay:cvc>
      </pay:card>
    </pay:CreateCardTokenRequest>
  </soap:Body>
</soap:Envelope>
```

**Success — HTTP 200:**
```xml
    <pay:CreateCardTokenResponse>
      <pay:cardToken>ADYC-8841-2296-0034</pay:cardToken>
    </pay:CreateCardTokenResponse>
```

Malformed card details → Fault `[702]`. Tokenization **succeeds even for test cards that later refuse** — refusal/failure behavior triggers at charge time, not tokenization time.

## Operation: `AuthoriseAndCapture`

**Request body element** (same envelope/header structure as above):
```xml
    <pay:AuthoriseAndCaptureRequest>
      <pay:merchantAccount>AtlasVoyages</pay:merchantAccount>
      <pay:merchantReference>BKG-448291</pay:merchantReference>
      <pay:amount>
        <pay:value>10.50</pay:value>
        <pay:currency>EUR</pay:currency>
      </pay:amount>
      <pay:cardToken>ADYC-8841-2296-0034</pay:cardToken>
    </pay:AuthoriseAndCaptureRequest>
```

**Success — HTTP 200:**
```xml
    <pay:AuthoriseAndCaptureResponse>
      <pay:pspReference>8816281234567890</pay:pspReference>
      <pay:resultCode>AUTHORISED</pay:resultCode>
      <pay:processedAt>06/08/2026 14:32:11 CET</pay:processedAt>
    </pay:AuthoriseAndCaptureResponse>
```
`pspReference` is Adyenta's transaction id — you need it for refunds. Note the date format.

**Refusal — HTTP 200 (not an error!):**
```xml
    <pay:AuthoriseAndCaptureResponse>
      <pay:pspReference>8816281234567891</pay:pspReference>
      <pay:resultCode>REFUSED</pay:resultCode>
      <pay:refusalCode>51</pay:refusalCode>
      <pay:refusalReason>Not enough balance</pay:refusalReason>
    </pay:AuthoriseAndCaptureResponse>
```
`refusalCode` values you must handle: `51` Not enough balance (soft), `43` Stolen card (hard — never retry this card anywhere), `05` Do not honour (soft).

**System error — HTTP 500, SOAP Fault:**
```xml
  <soap:Body>
    <soap:Fault>
      <faultcode>soap:Server</faultcode>
      <faultstring>[905] Internal error — transaction may not have been processed</faultstring>
    </soap:Fault>
  </soap:Body>
```
Fault codes: `[010]` authentication failure, `[702]` request validation failure (e.g. unsupported currency, malformed amount, unknown or non-Adyenta card token), `[905]` internal error.

## Operation: `RefundTransaction`

**Request body element:**
```xml
    <pay:RefundTransactionRequest>
      <pay:merchantAccount>AtlasVoyages</pay:merchantAccount>
      <pay:originalPspReference>8816281234567890</pay:originalPspReference>
      <pay:amount>
        <pay:value>10.50</pay:value>
        <pay:currency>EUR</pay:currency>
      </pay:amount>
    </pay:RefundTransactionRequest>
```
`amount` is required (Adyenta does not infer full refunds). Refunds reference the original transaction — no card token needed. Over-refunding an unknown or already-refunded `originalPspReference` → Fault `[702]`.

**Success — HTTP 200:** `resultCode` `REFUNDED` with a new `pspReference` for the refund.

## Operation: `GetTransactionStatus`

Request with `<pay:pspReference>` → `resultCode` of that transaction (`AUTHORISED`, `REFUSED`, or `REFUNDED`). Unknown reference → Fault `[702]`.

## Sandbox test cards

Behavior is keyed off the **card number** used in `CreateCardToken`, and triggers when the token is **charged** — the same test cards as your other vendor integrations, mapped to Adyenta's vocabulary:

| Test card number | Behavior at charge time |
|---|---|
| `4242 4242 4242 4242` | HTTP 200, `resultCode: AUTHORISED` |
| `4000 0000 0000 9995` | HTTP 200, `resultCode: REFUSED`, `refusalCode: 51` |
| `4000 0000 0000 9979` | HTTP 200, `resultCode: REFUSED`, `refusalCode: 43` |
| `4000 0000 0000 0119` | HTTP 500, SOAP Fault `[905]` |
| `4000 0000 0000 5900` | No response for 30 seconds, then Fault `[905]` |

Any other well-formed card number → treated as `4242 4242 4242 4242`.
