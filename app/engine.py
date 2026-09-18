"""Simulation engine: volume-conserving transfer of source-tagged aliquots.

Every sample's content is tracked as a mapping ``origin_sample_id -> volume``.
A transfer moves a proportional share of every origin's volume from source to
destination, so both total volume and total marker mass are conserved, and the
marker concentration of any mixture is the volume-weighted average of the
original samples' concentrations.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from .models import (
    AppliedTransfer,
    Issue,
    SampleState,
    SimulationRequest,
    SimulationResult,
    Snapshot,
    SourceContribution,
    StepResult,
)

# Tolerance for floating-point dust when comparing / cleaning up volumes.
EPS = 1e-9
# Decimal places used in reported numbers (1e-6 uL = 1 pL).
NDIGITS = 6


@dataclass
class _IssueDraft:
    code: str
    message: str
    transfer_index: int | None = None
    field: str | None = None
    context: dict[str, Any] = dc_field(default_factory=dict)

    def to_model(self) -> Issue:
        return Issue(
            code=self.code,  # type: ignore[arg-type]
            message=self.message,
            transfer_index=self.transfer_index,
            field=self.field,
            context=self.context,
        )


class SimulationError(Exception):
    """Raised when domain validation fails; carries precisely located issues."""

    def __init__(self, drafts: list[_IssueDraft]):
        self.issues: list[Issue] = [d.to_model() for d in drafts]
        super().__init__(self.issues[0].message if self.issues else "simulation failed")


def _r(x: float) -> float:
    """Round for output and normalise -0.0 to 0.0."""
    v = round(x, NDIGITS)
    return 0.0 if v == 0 else v


def simulate(request: SimulationRequest) -> SimulationResult:
    """Run one simulation, returning per-step snapshots.

    Raises :class:`SimulationError` with located issues when the request
    violates domain rules (unknown sample, time going backwards, or a
    transfer exceeding the source's current volume).
    """
    sample_order = [s.id for s in request.samples]
    known = set(sample_order)
    origin_conc = {s.id: s.marker_concentration_ng_per_ul for s in request.samples}

    # -- Phase 1: static checks over all transfers (collect every violation) --
    drafts: list[_IssueDraft] = []
    previous_time = None
    for i, t in enumerate(request.transfers):
        for field_name, sample_id in (("source", t.source), ("destination", t.destination)):
            if sample_id not in known:
                drafts.append(
                    _IssueDraft(
                        code="UNKNOWN_SAMPLE",
                        message=(
                            f"transfer #{i} references unknown {field_name} sample "
                            f"'{sample_id}'; declare it in 'samples'"
                        ),
                        transfer_index=i,
                        field=field_name,
                        context={"sample_id": sample_id},
                    )
                )
        if previous_time is not None and t.time < previous_time:
            drafts.append(
                _IssueDraft(
                    code="TIME_OUT_OF_ORDER",
                    message=(
                        f"transfer #{i} time {t.time.isoformat()} is earlier than the "
                        f"previous transfer time {previous_time.isoformat()}"
                    ),
                    transfer_index=i,
                    field="time",
                    context={
                        "time": t.time.isoformat(),
                        "previous_time": previous_time.isoformat(),
                    },
                )
            )
        previous_time = t.time
    if drafts:
        raise SimulationError(drafts)

    # -- Phase 2: simulate; volume feasibility can only be checked in order --
    state: dict[str, dict[str, float]] = {
        s.id: ({s.id: s.initial_volume_ul} if s.initial_volume_ul > 0 else {})
        for s in request.samples
    }

    def snapshot() -> Snapshot:
        total_volume = 0.0
        total_marker = 0.0
        sample_states: list[SampleState] = []
        for sid in sample_order:
            composition = state[sid]
            volume = sum(composition.values())
            marker = sum(v * origin_conc[origin] for origin, v in composition.items())
            total_volume += volume
            total_marker += marker
            if volume > EPS:
                sources = sorted(
                    (
                        SourceContribution(
                            source_id=origin,
                            volume_ul=_r(v),
                            fraction=_r(v / volume),
                        )
                        for origin, v in composition.items()
                        if v > EPS
                    ),
                    key=lambda c: (-c.volume_ul, c.source_id),
                )
                sample_states.append(
                    SampleState(
                        id=sid,
                        volume_ul=_r(volume),
                        marker_concentration_ng_per_ul=_r(marker / volume),
                        sources=sources,
                    )
                )
            else:
                sample_states.append(
                    SampleState(
                        id=sid,
                        volume_ul=0.0,
                        marker_concentration_ng_per_ul=None,
                        sources=[],
                    )
                )
        return Snapshot(
            total_volume_ul=_r(total_volume),
            total_marker_ng=_r(total_marker),
            samples=sample_states,
        )

    initial = snapshot()
    steps: list[StepResult] = []

    for i, t in enumerate(request.transfers):
        source_comp = state[t.source]
        available = sum(source_comp.values())
        if t.volume_ul > available + EPS:
            raise SimulationError(
                [
                    _IssueDraft(
                        code="INSUFFICIENT_VOLUME",
                        message=(
                            f"transfer #{i} requests {_r(t.volume_ul)} uL from sample "
                            f"'{t.source}' but only {_r(available)} uL is available"
                        ),
                        transfer_index=i,
                        field="volume_ul",
                        context={
                            "source": t.source,
                            "requested_volume_ul": _r(t.volume_ul),
                            "available_volume_ul": _r(available),
                        },
                    )
                ]
            )

        # Move the same proportion of every origin's volume.
        ratio = 1.0 if available <= EPS else min(t.volume_ul / available, 1.0)
        dest_comp = state[t.destination]
        for origin, v in list(source_comp.items()):
            moved = v * ratio
            remaining = v - moved
            if remaining <= EPS:
                del source_comp[origin]
            else:
                source_comp[origin] = remaining
            dest_comp[origin] = dest_comp.get(origin, 0.0) + moved

        steps.append(
            StepResult(
                transfer=AppliedTransfer(
                    index=i,
                    time=t.time,
                    source=t.source,
                    destination=t.destination,
                    volume_ul=t.volume_ul,
                ),
                snapshot=snapshot(),
            )
        )

    return SimulationResult(
        initial=initial,
        steps=steps,
        final=steps[-1].snapshot if steps else initial,
    )
