"""混样模拟核心引擎。

物理模型
--------

* 每次转移前假设源管内已充分混匀，转出的液体携带源管当前的混合浓度与
  来源构成；
* 体积守恒：``V_total = sum(各管体积)`` 全程不变；
* 标记物质量守恒：对每种标记物，``sum(管内体积 × 该管浓度)`` 全程不变；
* 来源占比按“来自各初始样本管的净体积 / 当前管内总体积”计算。

失败时（超量、时间逆序、未知样本）返回已完成的步骤、当时各管状态和
结构化错误信息，便于定位问题，不抛出业务异常。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import SampleIn, SimulationRequest, TransferIn, parse_time

# 浮点清理精度：小于该阈值的残余量按 0 处理
ZERO_EPS = 1e-12
ROUND_DIGITS = 12


class SimulationError(Exception):
    """输入层面的错误（理论上由校验器拦截，引擎内作为兜底）。"""

    def __init__(self, code: str, message: str, location: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.location = location


@dataclass
class _Tube:
    """引擎内部使用的管状态。"""

    id: str
    volume: float
    # 每种标记物的质量（浓度 × 体积），混匀计算都在质量域进行
    marker_amount: dict[str, float]
    # 来源构成：初始样本 id -> 净转入体积
    origins: dict[str, float] = field(default_factory=dict)


def _clean(value: float) -> float:
    """统一浮点输出：吞掉接近 0 的残差并截断噪声。"""
    if abs(value) < ZERO_EPS:
        return 0.0
    return round(value, ROUND_DIGITS)


def _concentrations(tube: _Tube, all_markers: list[str]) -> dict[str, float]:
    if tube.volume <= ZERO_EPS:
        return {name: 0.0 for name in all_markers}
    return {
        name: _clean(tube.marker_amount.get(name, 0.0) / tube.volume)
        for name in all_markers
    }


def _snapshot(tube: _Tube, all_markers: list[str]) -> dict:
    """对外的管状态视图（来源占比之和恒为 1，空管除外）。"""
    origins_out: list[dict] = []
    if tube.volume > ZERO_EPS:
        for source_id in sorted(tube.origins):
            vol = tube.origins[source_id]
            if vol <= ZERO_EPS:
                continue
            origins_out.append(
                {
                    "source_id": source_id,
                    "volume": _clean(vol),
                    "volume_fraction": _clean(vol / tube.volume),
                }
            )
    return {
        "id": tube.id,
        "volume": _clean(tube.volume),
        "concentrations": _concentrations(tube, all_markers),
        "origins": origins_out,
    }


def _total_marker_amount(
    tubes: dict[str, _Tube], all_markers: list[str]
) -> dict[str, float]:
    return {
        name: _clean(sum(t.marker_amount.get(name, 0.0) for t in tubes.values()))
        for name in all_markers
    }


def _conservation(
    before_vol: float,
    after_vol: float,
    before_amount: dict[str, float],
    after_amount: dict[str, float],
) -> dict:
    return {
        "volume_before": _clean(before_vol),
        "volume_after": _clean(after_vol),
        "volume_difference": _clean(after_vol - before_vol),
        "marker_amount_before": before_amount,
        "marker_amount_after": after_amount,
        "marker_amount_difference": {
            name: _clean(after_amount.get(name, 0.0) - before_amount.get(name, 0.0))
            for name in before_amount | after_amount
        },
    }


def _build_error(
    code: str, message: str, transfer_index: int, location: str
) -> dict:
    return {
        "code": code,
        "message": message,
        "transfer_index": transfer_index,
        "location": location,
    }


def run_simulation(request: SimulationRequest) -> dict:
    """执行整次模拟，返回可直接序列化的结果字典（含 status / error）。"""
    samples: list[SampleIn] = request.samples
    transfers: list[TransferIn] = request.transfers

    # 所有出现过的标记物取并集，保证每支管子的浓度字段完整
    marker_set: set[str] = set()
    for sample in samples:
        marker_set.update(sample.normalized_markers().keys())
    all_markers = sorted(marker_set)

    tubes: dict[str, _Tube] = {}
    for sample in samples:
        concentrations = sample.normalized_markers()
        tubes[sample.id] = _Tube(
            id=sample.id,
            volume=sample.initial_volume,
            marker_amount={
                name: concentrations.get(name, 0.0) * sample.initial_volume
                for name in all_markers
            },
            origins={sample.id: sample.initial_volume},
        )

    initial_states = [_snapshot(tubes[s.id], all_markers) for s in samples]
    initial_total_volume = sum(s.initial_volume for s in samples)
    initial_marker_amount = _total_marker_amount(tubes, all_markers)

    steps: list[dict] = []
    error: Optional[dict] = None
    last_time: Optional[float] = None

    for index, transfer in enumerate(transfers):
        loc = f"transfers[{index}]"

        if transfer.source == transfer.target:
            error = _build_error(
                "SAME_SOURCE_TARGET",
                f"转移的源管与目标管相同（均为 {transfer.source!r}），不允许自转移",
                index,
                loc,
            )
            break

        source = tubes.get(transfer.source)
        if source is None:
            error = _build_error(
                "UNKNOWN_SAMPLE",
                f"transfers[{index}] 引用了未在 samples 中声明的源管 "
                f"{transfer.source!r}",
                index,
                f"{loc}.source",
            )
            break

        target = tubes.get(transfer.target)
        if target is None:
            error = _build_error(
                "UNKNOWN_SAMPLE",
                f"transfers[{index}] 引用了未在 samples 中声明的目标管 "
                f"{transfer.target!r}",
                index,
                f"{loc}.target",
            )
            break

        current_time = parse_time(transfer.time)
        if last_time is not None and current_time < last_time - ZERO_EPS:
            error = _build_error(
                "TIME_NOT_ORDERED",
                f"transfers[{index}] 的时间 {transfer.time!r} 早于上一次转移的时间"
                f"（transfers[{index - 1}]），转移必须按时间非递减排序",
                index,
                f"{loc}.time",
            )
            break

        available = source.volume
        if transfer.amount > available + ZERO_EPS:
            error = _build_error(
                "INSUFFICIENT_VOLUME",
                f"transfers[{index}] 试图从 {transfer.source!r} 转出 "
                f"{transfer.amount}，但该管当前仅有 {_clean(available)}",
                index,
                f"{loc}.amount",
            )
            break

        amount = min(transfer.amount, available)

        # —— 转移前快照（用于守恒校验与转出液描述）——
        before_vol = sum(t.volume for t in tubes.values())
        before_amount = _total_marker_amount(tubes, all_markers)
        source_concentrations = _concentrations(source, all_markers)

        # 转出液的来源构成：按源管各来源占比分摊转出体积
        transferred_origins: list[dict] = []
        if available > ZERO_EPS:
            for src_id in sorted(source.origins):
                share = source.origins[src_id] / available * amount
                if share <= ZERO_EPS:
                    continue
                transferred_origins.append(
                    {
                        "source_id": src_id,
                        "volume": _clean(share),
                        "volume_fraction": _clean(share / amount) if amount else 0.0,
                    }
                )

        # —— 更新质量与体积 ——
        for name in all_markers:
            moved_mass = source_concentrations[name] * amount
            source.marker_amount[name] -= moved_mass
            target.marker_amount[name] += moved_mass

        source.volume -= amount
        target.volume += amount

        # —— 更新来源构成（源管按比例扣减，目标管累加）——
        if available > ZERO_EPS:
            ratio = amount / available
            for src_id, src_vol in list(source.origins.items()):
                share = src_vol * ratio
                source.origins[src_id] = src_vol - share
                target.origins[src_id] = target.origins.get(src_id, 0.0) + share

        after_vol = sum(t.volume for t in tubes.values())
        after_amount = _total_marker_amount(tubes, all_markers)

        steps.append(
            {
                "index": index,
                "transfer_id": transfer.id,
                "time": transfer.time,
                "source": transfer.source,
                "target": transfer.target,
                "amount": _clean(amount),
                "transferred": {
                    "volume": _clean(amount),
                    "concentrations": {
                        name: _clean(source_concentrations[name]) for name in all_markers
                    },
                    "origins": transferred_origins,
                },
                "source_after": _snapshot(source, all_markers),
                "target_after": _snapshot(target, all_markers),
                "conservation": _conservation(
                    before_vol, after_vol, before_amount, after_amount
                ),
            }
        )
        last_time = current_time

    final_states = [_snapshot(tubes[s.id], all_markers) for s in samples]
    final_total_volume = sum(t.volume for t in tubes.values())
    final_marker_amount = _total_marker_amount(tubes, all_markers)

    conservation = {
        "initial_total_volume": _clean(initial_total_volume),
        "final_total_volume": _clean(final_total_volume),
        "volume_difference": _clean(final_total_volume - initial_total_volume),
        "initial_marker_amount": initial_marker_amount,
        "final_marker_amount": final_marker_amount,
        "marker_amount_difference": {
            name: _clean(
                final_marker_amount.get(name, 0.0)
                - initial_marker_amount.get(name, 0.0)
            )
            for name in all_markers
        },
    }

    return {
        "status": "failed" if error else "success",
        "name": request.name,
        "initial_states": initial_states,
        "steps": steps,
        "final_states": final_states,
        "conservation": conservation,
        "error": error,
    }
