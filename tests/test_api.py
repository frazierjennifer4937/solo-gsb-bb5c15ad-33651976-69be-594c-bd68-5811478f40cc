"""API 端到端测试：创建、查询、列表、错误定位、持久化。"""
import pytest


VALID_PAYLOAD = {
    "name": "api-demo",
    "samples": [
        {"id": "A", "initial_volume": 100.0, "concentration": 10.0},
        {"id": "B", "initial_volume": 100.0, "concentration": 0.0},
    ],
    "transfers": [
        {"time": 1, "source": "A", "target": "B", "amount": 40.0},
    ],
}


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_simulation_success(client):
    response = client.post("/api/simulations", json=VALID_PAYLOAD)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "success"
    assert body["error"] is None
    assert body["id"]
    assert body["created_at"]
    assert len(body["steps"]) == 1
    assert body["conservation"]["volume_difference"] == 0


def test_query_simulation_after_create(client):
    created = client.post("/api/simulations", json=VALID_PAYLOAD).json()
    response = client.get(f"/api/simulations/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_query_missing_simulation_404(client):
    response = client.get("/api/simulations/does-not-exist")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "SIMULATION_NOT_FOUND"
    assert detail["location"] == "path.sim_id"


def test_failed_simulation_persisted_and_queryable(client):
    payload = {
        "samples": [
            {"id": "A", "initial_volume": 5.0, "concentration": 1.0},
            {"id": "B", "initial_volume": 5.0, "concentration": 0.0},
        ],
        "transfers": [
            {"time": 1, "source": "A", "target": "B", "amount": 100.0},
        ],
    }
    created = client.post("/api/simulations", json=payload)
    assert created.status_code == 400
    body = created.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "INSUFFICIENT_VOLUME"
    assert body["error"]["location"] == "transfers[0].amount"

    # 失败记录同样落库，GET 返回 400 + 完整内容
    fetched = client.get(f"/api/simulations/{body['id']}")
    assert fetched.status_code == 400
    assert fetched.json()["error"]["code"] == "INSUFFICIENT_VOLUME"


def test_unknown_sample_error(client):
    payload = {
        "samples": [{"id": "A", "initial_volume": 5.0, "concentration": 1.0}],
        "transfers": [
            {"time": 1, "source": "A", "target": "X", "amount": 1.0},
        ],
    }
    response = client.post("/api/simulations", json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNKNOWN_SAMPLE"


def test_time_reversal_error(client):
    payload = {
        "samples": [
            {"id": "A", "initial_volume": 10.0, "concentration": 1.0},
            {"id": "B", "initial_volume": 10.0, "concentration": 0.0},
        ],
        "transfers": [
            {"time": 2, "source": "A", "target": "B", "amount": 1.0},
            {"time": 1, "source": "A", "target": "B", "amount": 1.0},
        ],
    }
    response = client.post("/api/simulations", json=payload)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "TIME_NOT_ORDERED"
    assert error["transfer_index"] == 1


def test_validation_error_for_bad_shape(client):
    # 缺少 concentration / markers
    payload = {
        "samples": [{"id": "A", "initial_volume": 10.0}],
        "transfers": [],
    }
    response = client.post("/api/simulations", json=payload)
    assert response.status_code == 422
    # 标准 FastAPI 错误结构，定位到具体字段
    locs = [".".join(str(x) for x in e["loc"]) for e in response.json()["detail"]]
    assert any("samples" in loc for loc in locs)


def test_validation_negative_amount(client):
    payload = {
        "samples": [
            {"id": "A", "initial_volume": 10.0, "concentration": 1.0},
            {"id": "B", "initial_volume": 10.0, "concentration": 0.0},
        ],
        "transfers": [{"time": 1, "source": "A", "target": "B", "amount": -2.0}],
    }
    response = client.post("/api/simulations", json=payload)
    assert response.status_code == 422
    locs = [".".join(str(x) for x in e["loc"]) for e in response.json()["detail"]]
    assert any(loc.endswith("amount") for loc in locs)


def test_duplicate_sample_ids_rejected(client):
    payload = {
        "samples": [
            {"id": "A", "initial_volume": 10.0, "concentration": 1.0},
            {"id": "A", "initial_volume": 5.0, "concentration": 2.0},
        ],
        "transfers": [],
    }
    response = client.post("/api/simulations", json=payload)
    assert response.status_code == 422


def test_list_simulations_and_status_filter(client):
    client.post("/api/simulations", json=VALID_PAYLOAD)
    failed_payload = {
        "samples": [
            {"id": "A", "initial_volume": 1.0, "concentration": 1.0},
            {"id": "B", "initial_volume": 1.0, "concentration": 0.0},
        ],
        "transfers": [{"time": 1, "source": "A", "target": "B", "amount": 2.0}],
    }
    client.post("/api/simulations", json=failed_payload)

    all_items = client.get("/api/simulations").json()["items"]
    assert len(all_items) == 2

    failed_only = client.get("/api/simulations", params={"status": "failed"}).json()
    assert len(failed_only["items"]) == 1
    assert failed_only["items"][0]["status"] == "failed"
    assert failed_only["items"][0]["error_code"] == "INSUFFICIENT_VOLUME"

    success_only = client.get("/api/simulations", params={"status": "success"}).json()
    assert len(success_only["items"]) == 1


def test_pagination(client):
    for _ in range(3):
        client.post("/api/simulations", json=VALID_PAYLOAD)
    page = client.get("/api/simulations", params={"limit": 2, "offset": 0}).json()
    assert len(page["items"]) == 2
    assert page["limit"] == 2
