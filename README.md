# VGS payment gateway for Atlas Voyages

One API for charges and refunds. Behind it, each line item of a travel package is
routed to a vendor by region, retried once at the other vendor on a soft failure,
and rolled back if the package cannot complete. Every movement of money lands in
a ledger. Built live in a 90 minute session; see `SPEC.md` for the requirements
and decisions.

## Run

```bash
uv sync
./run.sh          # Stripely mock :4001, Adyenta mock :4002, gateway :8000
uv run pytest     # 11 end-to-end tests, needs run.sh up
```

Demo page: http://localhost:8000. Interactive API docs: http://localhost:8000/docs.
Exported spec: `docs/openapi.json`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/payments` | Charge a package of 1 to 5 line items. Requires `Idempotency-Key`. |
| GET | `/v1/payments/{id}` | Fetch a payment and its per item attempts. |
| POST | `/v1/refunds` | Refund one line item (full or partial) or the whole bundle. |
| GET | `/v1/refunds/failed` | Refunds the vendor did not confirm. The ops feed. |
| GET | `/v1/ledger` | Gross, refunds, fees, net per vendor and currency, plus vendor success by region. |
| GET | `/v1/ledger.csv` | Same rows as CSV, the finance feed. |

## Layout

```
gateway/main.py      API, validation, idempotency
gateway/router.py    routing, one retry, all-or-nothing rollback
gateway/vendors.py   Stripely (JSON) and Adyenta (SOAP) adapters, one shared result type
gateway/ledger.py    append-only ledger and totals
mocks/               both vendor sandboxes per docs/, plus an outage switch on Adyenta
static/index.html    demo page
tests/test_flow.py   end-to-end tests
docs/                vendor specs from the brief, exported OpenAPI spec
```

## Test cards

Same five at both vendors: `4242...` succeeds, `...9995` soft decline,
`...9979` stolen card (never retried), `...0119` vendor error, `...5900` hangs
(gateway times out at 5 seconds).
