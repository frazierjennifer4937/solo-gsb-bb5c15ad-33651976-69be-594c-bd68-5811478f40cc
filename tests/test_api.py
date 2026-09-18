"""API and engine tests (run with: python -m pytest)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Fresh app instance backed by a throwaway SQLite database."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def demo_payload() -> dict:
    """A -> C -> back to A, plus B -> C. Total 1500 uL, 2400 ng marker."""
    return {
        "name": "demo-run",
        "samples": [
            {"id": "A", "initial_volume_ul": 1000.0, "marker_concentration_ng_per_ul": 2.0},
            {"id": "B", "initial_volume_ul": 500.0, "marker_concentration_ng_per_ul": 0.8},
            {"id": "C", "initial_volume_ul": 0.0, "marker_concentration_ng_per_ul": 0.0},
        ],
        "transfers": [
            {"time": "2026-09-18T09:00:00Z", "source": "A", "destination": "C", "volume_ul": 200.0},
            {"time": "2026-09-18T09:05:00Z", "source": "B", "destination": "C", "volume_ul": 100.0},
            {"time": "2026-09-18T09:10:00Z", "source": "C", "destination": "A", "volume_ul": 150.0},
        ],
    }


def sample_by_id(snapshot: dict, sample_id: str) -> dict:
    return next(s for s in snapshot["samples"] if s["id"] == sample_id)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_successful_simulation_math_and_conservation(client):
    resp = client.post("/api/v1/simulations", json=demo_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["status"] == "completed"
    assert body["name"] == "demo-run"
    assert body["errors"] is None
    result = body["result"]
    assert len(result["steps"]) == 3

    # Initial state.
    assert result["initial"]["total_volume_ul"] == 1500.0
    assert result["initial"]["total_marker_ng"] == 2400.0
    assert sample_by_id(result["initial"], "C")["marker_concentration_ng_per_ul"] is None

    # After t1: A -> C 200 uL of pure A material.
    step1 = result["steps"][0]["snapshot"]
    assert sample_by_id(step1, "A")["volume_ul"] == 800.0
    c1 = sample_by_id(step1, "C")
    assert c1["volume_ul"] == 200.0
    assert c1["marker_concentration_ng_per_ul"] == 2.0
    assert c1["sources"] == [{"source_id": "A", "volume_ul": 200.0, "fraction": 1.0}]

    # After t2: C holds 200 uL from A + 100 uL from B -> 480 ng / 300 uL = 1.6 ng/uL.
    step2 = result["steps"][1]["snapshot"]
    c2 = sample_by_id(step2, "C")
    assert c2["volume_ul"] == 300.0
    assert c2["marker_concentration_ng_per_ul"] == pytest.approx(1.6)
    sources2 = {s["source_id"]: s for s in c2["sources"]}
    assert sources2["A"]["fraction"] == pytest.approx(2 / 3, abs=1e-6)
    assert sources2["B"]["fraction"] == pytest.approx(1 / 3, abs=1e-6)

    # After t3: half of C (proportional) goes back to A: 100 uL of A-origin
    # and 50 uL of B-origin material, so A ends at 950 uL / 1840 ng.
    final = result["final"]
    assert final["total_volume_ul"] == 1500.0
    assert final["total_marker_ng"] == 2400.0
    a_final = sample_by_id(final, "A")
    assert a_final["volume_ul"] == 950.0
    assert a_final["marker_concentration_ng_per_ul"] == pytest.approx(1840 / 950, abs=1e-6)
    a_sources = {s["source_id"]: s for s in a_final["sources"]}
    assert a_sources["A"]["volume_ul"] == 900.0
    assert a_sources["B"]["volume_ul"] == 50.0
    c_final = sample_by_id(final, "C")
    assert c_final["volume_ul"] == 150.0
    sources_final = {s["source_id"]: s for s in c_final["sources"]}
    assert sources_final["A"]["volume_ul"] == 100.0
    assert sources_final["B"]["volume_ul"] == 50.0
    assert sample_by_id(final, "B")["volume_ul"] == 400.0

    # Volume conserved at every step.
    for step in result["steps"]:
        assert step["snapshot"]["total_volume_ul"] == 1500.0
        assert step["snapshot"]["total_marker_ng"] == 2400.0


def test_empty_transfers_is_allowed(client):
    payload = demo_payload()
    payload["transfers"] = []
    resp = client.post("/api/v1/simulations", json=payload)
    assert resp.status_code == 201
    result = resp.json()["result"]
    assert result["steps"] == []
    assert result["final"] == result["initial"]


def test_naive_timestamps_treated_as_utc(client):
    payload = demo_payload()
    for t in payload["transfers"]:
        t["time"] = t["time"].replace("Z", "")
    resp = client.post("/api/v1/simulations", json=payload)
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Domain errors: precise location
# ---------------------------------------------------------------------------


def test_unknown_sample_is_located(client):
    payload = demo_payload()
    payload["transfers"][1]["source"] = "ghost"
    resp = client.post("/api/v1/simulations", json=payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["errors"][0]["code"] == "UNKNOWN_SAMPLE"
    assert body["errors"][0]["transfer_index"] == 1
    assert body["errors"][0]["field"] == "source"
    assert body["errors"][0]["context"]["sample_id"] == "ghost"

    # The failed run is persisted and queryable.
    run_id = body["run_id"]
    stored = client.get(f"/api/v1/simulations/{run_id}")
    assert stored.status_code == 200
    assert stored.json()["status"] == "failed"
    assert stored.json()["errors"][0]["code"] == "UNKNOWN_SAMPLE"
    assert stored.json()["result"] is None


def test_unknown_destination_is_located(client):
    payload = demo_payload()
    payload["transfers"][0]["destination"] = "nowhere"
    resp = client.post("/api/v1/simulations", json=payload)
    assert resp.status_code == 422
    err = resp.json()["errors"][0]
    assert err["code"] == "UNKNOWN_SAMPLE"
    assert err["transfer_index"] == 0
    assert err["field"] == "destination"


def test_multiple_unknown_samples_all_reported(client):
    payload = demo_payload()
    payload["transfers"][0]["source"] = "x"
    payload["transfers"][2]["destination"] = "y"
    resp = client.post("/api/v1/simulations", json=payload)
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert [e["transfer_index"] for e in errors] == [0, 2]


def test_time_out_of_order_is_located(client):
    payload = demo_payload()
    payload["transfers"][2]["time"] = "2026-09-18T08:00:00Z"  # before transfer #1
    resp = client.post("/api/v1/simulations", json=payload)
    assert resp.status_code == 422
    err = resp.json()["errors"][0]
    assert err["code"] == "TIME_OUT_OF_ORDER"
    assert err["transfer_index"] == 2
    assert err["field"] == "time"
    assert err["context"]["previous_time"] == "2026-09-18T09:05:00+00:00"


def test_insufficient_volume_is_located(client):
    payload = demo_payload()
    payload["transfers"][0]["volume_ul"] = 1500.0  # A only holds 1000
    resp = client.post("/api/v1/simulations", json=payload)
    assert resp.status_code == 422
    err = resp.json()["errors"][0]
    assert err["code"] == "INSUFFICIENT_VOLUME"
    assert err["transfer_index"] == 0
    assert err["field"] == "volume_ul"
    assert err["context"]["requested_volume_ul"] == 1500.0
    assert err["context"]["available_volume_ul"] == 1000.0


def test_insufficient_volume_after_prior_transfers(client):
    """Availability is evaluated against the *current* volume, not the initial one."""
    payload = demo_payload()
    payload["transfers"].append(
        {"time": "2026-09-18T09:20:00Z", "source": "B", "destination": "A", "volume_ul": 450.0}
    )  # B holds only 400 by then
    resp = client.post("/api/v1/simulations", json=payload)
    assert resp.status_code == 422
    err = resp.json()["errors"][0]
    assert err["code"] == "INSUFFICIENT_VOLUME"
    assert err["transfer_index"] == 3
    assert err["context"]["available_volume_ul"] == 400.0


def test_transferring_entire_volume_empties_source(client):
    payload = demo_payload()
    payload["transfers"] = [
        {"time": "2026-09-18T09:00:00Z", "source": "B", "destination": "C", "volume_ul": 500.0}
    ]
    resp = client.post("/api/v1/simulations", json=payload)
    assert resp.status_code == 201
    final = resp.json()["result"]["final"]
    b = sample_by_id(final, "B")
    assert b["volume_ul"] == 0.0
    assert b["marker_concentration_ng_per_ul"] is None
    assert b["sources"] == []


# ---------------------------------------------------------------------------
# Schema validation (Pydantic)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transfer_patch",
    [{"volume_ul": 0}, {"volume_ul": -5}, {"source": "A", "destination": "A"}],
)
def test_invalid_transfer_rejected(client, transfer_patch):
    payload = demo_payload()
    payload["transfers"][0].update(transfer_patch)
    resp = client.post("/api/v1/simulations", json=payload)
    assert resp.status_code == 422


def test_duplicate_sample_ids_rejected(client):
    payload = demo_payload()
    payload["samples"].append(
        {"id": "A", "initial_volume_ul": 1.0, "marker_concentration_ng_per_ul": 1.0}
    )
    resp = client.post("/api/v1/simulations", json=payload)
    assert resp.status_code == 422


def test_empty_samples_rejected(client):
    payload = demo_payload()
    payload["samples"] = []
    resp = client.post("/api/v1/simulations", json=payload)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Query endpoints
# ---------------------------------------------------------------------------


def test_get_completed_run_roundtrip(client):
    created = client.post("/api/v1/simulations", json=demo_payload()).json()
    resp = client.get(f"/api/v1/simulations/{created['run_id']}")
    assert resp.status_code == 200
    fetched = resp.json()
    assert fetched["run_id"] == created["run_id"]
    assert fetched["status"] == "completed"
    assert fetched["result"]["final"]["total_volume_ul"] == 1500.0
    assert fetched["request"]["samples"][0]["id"] == "A"


def test_get_unknown_run_returns_404(client):
    resp = client.get("/api/v1/simulations/does-not-exist")
    assert resp.status_code == 404


def test_list_runs(client):
    client.post("/api/v1/simulations", json=demo_payload())
    bad = demo_payload()
    bad["transfers"][0]["source"] = "ghost"
    client.post("/api/v1/simulations", json=bad)

    resp = client.get("/api/v1/simulations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    statuses = {item["status"] for item in body["items"]}
    assert statuses == {"completed", "failed"}
    assert all(item["sample_count"] == 3 for item in body["items"])
    assert all(item["transfer_count"] == 3 for item in body["items"])

    page = client.get("/api/v1/simulations?limit=1&offset=1").json()
    assert page["total"] == 2
    assert len(page["items"]) == 1
