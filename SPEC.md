# VGS payment gateway for Atlas Voyages

Session start: 2026-09-25 16:36 PDT. Budget 90 minutes.

## Problem

Atlas sells travel packages. A customer pays once, but Atlas charges up to five
suppliers (flight, hotel, car, insurance, excursion) through different processors.
Each processor has its own API and failure modes. Atlas wants one API for charges
and refunds. VGS routes each charge to a vendor, retries at another vendor on
failure, backs everything out if the package cannot complete, and keeps a ledger.

## What the stakeholder said

- Scope is Atlas's checkout backend only. Finance just needs a data feed they
  can load into Tableau later.
- One package payment fans out to 3 to 5 vendor charges.
- Retry once at every vendor. If any charge hard-fails, refund every charge
  that succeeded. Large amounts, so all or nothing.
- Ideal is authorize everywhere first, then capture everywhere.
- Routing today is by region: US goes to Stripely, EU goes to Adyenta. For
  APAC, rotate between vendors and track which one succeeds more.
- Atlas collects raw card details and sends them to us. They accept PCI scope.
- Two vendors now, more later.

## The one flow that must work

1. Atlas posts a package with a card and 3 line items to `POST /v1/payments`.
2. Gateway tokenizes the card at each vendor it needs, never storing the number.
3. Each line item is charged at its routed vendor. On a soft failure it is
   retried once at the other vendor.
4. If any line item still fails, every succeeded charge is refunded and the
   package returns `failed`. Otherwise it returns `succeeded`.
5. `POST /v1/refunds` refunds a line item, full or partial.
6. `GET /v1/ledger` shows gross, refunds, fees, and net per vendor and currency.

## Decisions

Auth then capture. The vendor specs only expose charge-and-capture in one call,
so there is no separate authorize step. Assumption: emulate all-or-nothing with
charge plus compensating refund. The alternative, holding funds, does not exist
in these APIs.

Retry rules. Soft declines and vendor errors retry once at the other vendor.
Stolen card is a hard decline and never retries anywhere. The alternative,
retrying everything, would resubmit stolen cards.

Vendor 500 or timeout. The vendor may have taken the money. The attempt is
recorded as `unknown` for reconciliation and the line item fails over. The
alternative, treating it as a clean failure, loses money silently.

Idempotency. Atlas sends an `Idempotency-Key` header. A replay returns the
original response and never charges again. Adyenta has no idempotency, so this
layer is the only thing stopping double charges there. The alternative, keying
on booking reference, breaks when a booking has a legitimate second charge.

Money. Amounts are integers in minor units with uppercase currency. Adapters
convert to each vendor's format. The alternative, floats, rounds cents away.

Storage. In memory for the demo. The alternative, SQLite, adds files to explain
and nothing to the demo.

Stack. FastAPI for the gateway and both vendor mocks, one HTML page for the
demo. Few files, easy to read aloud.

## What was built

- `gateway/main.py` API: `POST /v1/payments`, `GET /v1/payments/{id}`,
  `POST /v1/refunds`, `GET /v1/ledger`, `GET /v1/ledger.csv`, demo page at `/`.
- `gateway/router.py` routing, one retry at the other vendor, rollback.
- `gateway/vendors.py` Stripely and Adyenta adapters with one shared result type.
- `gateway/ledger.py` append-only rows, per vendor and currency totals, vendor
  success rates by region.
- `mocks/` both vendor sandboxes, plus a sandbox-only outage switch on Adyenta.
- `tests/test_flow.py` nine end-to-end tests. Run `./run.sh` then `uv run pytest`.

Vendor calls time out after 5 seconds, so the hanging test card becomes an
`unknown` attempt instead of burning the fare hold. A tokenize failure skips
that vendor without a reconciliation flag, since no money moved.

## Out of scope

Real vendor accounts, FX conversion, webhooks, the Tableau feed itself
(a CSV export stands in for it), authentication on our own API.

## Test cards

Same five cards at both vendors: 4242 succeeds, 9995 soft decline, 9979 stolen,
0119 vendor error, 5900 hangs 30 seconds then errors.
