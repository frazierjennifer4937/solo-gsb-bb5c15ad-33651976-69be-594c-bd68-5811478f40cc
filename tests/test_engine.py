"""引擎层测试：混合浓度、来源占比、守恒与各类业务错误。"""
from app.engine import run_simulation
from app.models import SampleIn, SimulationRequest, TransferIn


def make_request():
    return SimulationRequest(
        name="demo",
        samples=[
            SampleIn(id="A", initial_volume=100.0, concentration=10.0),
            SampleIn(id="B", initial_volume=100.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time=1, source="A", target="B", amount=40.0),
            TransferIn(time=2, source="B", target="A", amount=70.0),
        ],
    )


def test_basic_mixing_concentrations():
    result = run_simulation(make_request())

    assert result["status"] == "success"
    assert len(result["steps"]) == 2

    # 第一步：A(100, c=10) -> B 转移 40
    # B 变为 140，质量 400 => 浓度 400/140 = 2.8571...
    step1 = result["steps"][0]
    assert step1["source_after"]["volume"] == 60
    assert step1["source_after"]["concentrations"]["default"] == 10.0
    assert step1["target_after"]["volume"] == 140
    assert abs(step1["target_after"]["concentrations"]["default"] - 400 / 140) < 1e-9

    # 第二步：B(140, c=2.8571) -> A 转移 70（转出 B 的一半）
    step2 = result["steps"][1]
    assert step2["source_after"]["volume"] == 70
    assert step2["target_after"]["volume"] == 130
    # A：质量 600 + 200 = 800，体积 130
    assert abs(step2["target_after"]["concentrations"]["default"] - 800 / 130) < 1e-9
    # B 剩余：质量 200，体积 70
    assert abs(step2["source_after"]["concentrations"]["default"] - 200 / 70) < 1e-9


def test_origin_fractions_and_conservation():
    result = run_simulation(make_request())

    final_a = next(t for t in result["final_states"] if t["id"] == "A")
    final_b = next(t for t in result["final_states"] if t["id"] == "B")

    # 第二步后 A 的来源：原 A 剩 60 + 从 B 回流入 A 的 20(A)+50(B)
    # => A 来源 80，B 来源 50，总体积 130
    origins_a = {o["source_id"]: o for o in final_a["origins"]}
    assert abs(origins_a["A"]["volume"] - 80) < 1e-9
    assert abs(origins_a["B"]["volume"] - 50) < 1e-9
    assert abs(origins_a["A"]["volume_fraction"] - 80 / 130) < 1e-9
    assert abs(sum(o["volume_fraction"] for o in final_a["origins"]) - 1.0) < 1e-9

    # B 剩余：A 20 + B 50
    origins_b = {o["source_id"]: o for o in final_b["origins"]}
    assert abs(origins_b["A"]["volume"] - 20) < 1e-9
    assert abs(origins_b["B"]["volume"] - 50) < 1e-9

    conservation = result["conservation"]
    assert conservation["initial_total_volume"] == 200
    assert conservation["final_total_volume"] == 200
    assert conservation["volume_difference"] == 0
    assert conservation["initial_marker_amount"]["default"] == 1000
    assert conservation["final_marker_amount"]["default"] == 1000
    assert conservation["marker_amount_difference"]["default"] == 0

    for step in result["steps"]:
        assert step["conservation"]["volume_difference"] == 0
        assert step["conservation"]["marker_amount_difference"]["default"] == 0


def test_multi_marker_mixing():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, markers={"X": 2.0, "Y": 8.0}),
            SampleIn(id="B", initial_volume=10.0, markers={"X": 6.0}),
        ],
        transfers=[TransferIn(time=1, source="A", target="B", amount=10.0)],
    )
    result = run_simulation(request)
    assert result["status"] == "success"

    b = result["steps"][0]["target_after"]
    # B：X 质量 60 + 20 = 80；Y 质量 80；体积 20
    assert b["concentrations"]["X"] == 4.0
    assert b["concentrations"]["Y"] == 4.0
    # A 已空，所有标记物浓度按 0 返回
    a = result["steps"][0]["source_after"]
    assert a["volume"] == 0
    assert a["concentrations"] == {"X": 0.0, "Y": 0.0}
    assert a["origins"] == []


def test_full_transfer_conservation():
    request = SimulationRequest(
        samples=[SampleIn(id="A", initial_volume=5.0, concentration=3.0)],
        transfers=[],  # 空操作序列也合法
    )
    result = run_simulation(request)
    assert result["status"] == "success"
    assert result["steps"] == []
    assert result["conservation"]["volume_difference"] == 0


def test_insufficient_volume_error():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=10.0, concentration=0.0),
        ],
        transfers=[TransferIn(time=1, source="A", target="B", amount=11.0)],
    )
    result = run_simulation(request)
    assert result["status"] == "failed"
    error = result["error"]
    assert error["code"] == "INSUFFICIENT_VOLUME"
    assert error["transfer_index"] == 0
    assert error["location"] == "transfers[0].amount"
    # 失败时不产生步骤，状态停留在初始
    assert result["steps"] == []


def test_unknown_source_error_location():
    request = SimulationRequest(
        samples=[SampleIn(id="A", initial_volume=10.0, concentration=1.0)],
        transfers=[TransferIn(time=1, source="C", target="A", amount=1.0)],
    )
    result = run_simulation(request)
    assert result["error"]["code"] == "UNKNOWN_SAMPLE"
    assert result["error"]["location"] == "transfers[0].source"


def test_unknown_target_error_location():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=5.0, concentration=0.0),
        ],
        transfers=[TransferIn(time=1, source="A", target="C", amount=1.0)],
    )
    result = run_simulation(request)
    assert result["error"]["code"] == "UNKNOWN_SAMPLE"
    assert result["error"]["location"] == "transfers[0].target"


def test_time_reversal_error():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=10.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time=2, source="A", target="B", amount=1.0),
            TransferIn(time=1, source="A", target="B", amount=1.0),
        ],
    )
    result = run_simulation(request)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "TIME_NOT_ORDERED"
    assert result["error"]["transfer_index"] == 1
    assert result["error"]["location"] == "transfers[1].time"
    # 第一步成功保留
    assert len(result["steps"]) == 1


def test_iso_time_ordering_supported():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=10.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time="2026-09-18T10:00:00Z", source="A", target="B", amount=1.0),
            TransferIn(time="2026-09-18T11:00:00Z", source="A", target="B", amount=1.0),
        ],
    )
    result = run_simulation(request)
    assert result["status"] == "success"


def test_same_source_target_rejected():
    request = SimulationRequest(
        samples=[SampleIn(id="A", initial_volume=10.0, concentration=1.0)],
        transfers=[TransferIn(time=1, source="A", target="A", amount=1.0)],
    )
    result = run_simulation(request)
    assert result["error"]["code"] == "SAME_SOURCE_TARGET"
