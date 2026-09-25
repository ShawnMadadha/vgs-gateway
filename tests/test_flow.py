"""End-to-end tests against the running mocks and gateway. Start them with ./run.sh first."""
import uuid

import httpx
import pytest

GW = "http://localhost:8000"
ADYENTA = "http://localhost:4002"
GOOD, SOFT, STOLEN, ERROR = "4242424242424242", "4000000000009995", "4000000000009979", "4000000000000119"


def package(card: str, ref: str = "BKG-T") -> dict:
    return {"booking_reference": ref, "card": {"number": card, "exp_month": 8, "exp_year": 2029, "cvc": "123"},
            "line_items": [{"id": "flight", "amount": 45000, "currency": "USD", "region": "US"},
                           {"id": "hotel", "amount": 32000, "currency": "EUR", "region": "EU"},
                           {"id": "tour", "amount": 9900, "currency": "USD", "region": "APAC"}]}


def pay(body: dict, key: str | None = None) -> httpx.Response:
    return httpx.post(f"{GW}/v1/payments", json=body, headers={"Idempotency-Key": key or uuid.uuid4().hex}, timeout=30)


@pytest.fixture(autouse=True)
def adyenta_up():
    yield
    httpx.post(f"{ADYENTA}/__sandbox/outage", params={"on": "false"})


def test_happy_path_routes_by_region():
    r = pay(package(GOOD))
    assert r.status_code == 201
    items = {i["id"]: i for i in r.json()["line_items"]}
    assert items["flight"]["vendor"] == "stripely" and items["hotel"]["vendor"] == "adyenta"
    assert all(i["status"] == "charged" for i in items.values())
    assert "number" not in r.text and "cvc" not in r.text  # card details never come back


def test_replay_returns_same_payment_without_new_charges():
    key = uuid.uuid4().hex
    first, second = pay(package(GOOD), key), pay(package(GOOD), key)
    assert first.json()["id"] == second.json()["id"]
    rows = httpx.get(f"{GW}/v1/ledger").json()["rows"]
    assert sum(r["payment_id"] == first.json()["id"] and r["kind"] == "charge" for r in rows) == 3


def test_replay_with_different_body_is_rejected():
    key = uuid.uuid4().hex
    pay(package(GOOD), key)
    assert pay(package(GOOD, ref="BKG-OTHER"), key).status_code == 409


def test_soft_decline_retries_at_other_vendor_then_fails():
    r = pay(package(SOFT))
    assert r.status_code == 402
    flight = r.json()["line_items"][0]
    assert [a["vendor"] for a in flight["attempts"]] == ["stripely", "adyenta"]
    assert r.json()["line_items"][1]["status"] == "not_attempted"


def test_stolen_card_never_retries():
    r = pay(package(STOLEN))
    assert r.status_code == 402
    assert [a["outcome"] for a in r.json()["line_items"][0]["attempts"]] == ["hard_decline"]


def test_vendor_error_is_flagged_for_reconciliation():
    r = pay(package(ERROR))
    assert r.status_code == 402
    assert {x["vendor"] for x in r.json()["needs_reconciliation"]} == {"stripely", "adyenta"}


def test_vendor_outage_rolls_back_successful_charges():
    httpx.post(f"{ADYENTA}/__sandbox/outage", params={"on": "true"})
    r = pay(package(GOOD))
    assert r.status_code == 402
    items = {i["id"]: i for i in r.json()["line_items"]}
    assert items["flight"]["status"] == "rolled_back"  # Stripely charge succeeded, then was refunded
    assert items["hotel"]["status"] == "failed" and items["tour"]["status"] == "not_attempted"
    assert "needs_reconciliation" not in r.json()  # tokenize failed, so no money moved at Adyenta


def test_partial_refund_then_over_refund_rejected():
    p = pay(package(GOOD)).json()
    r = httpx.post(f"{GW}/v1/refunds", headers={"Idempotency-Key": uuid.uuid4().hex},
                   json={"payment_id": p["id"], "line_item_id": "hotel", "amount": 10000})
    assert r.status_code == 200 and r.json()["remaining"] == 22000
    r = httpx.post(f"{GW}/v1/refunds", headers={"Idempotency-Key": uuid.uuid4().hex},
                   json={"payment_id": p["id"], "line_item_id": "hotel", "amount": 30000})
    assert r.status_code == 409


def test_ledger_net_is_gross_minus_refunds_minus_fees():
    for row in httpx.get(f"{GW}/v1/ledger").json()["summary"]:
        assert row["net"] == row["gross"] - row["refunds"] - row["fees"]
