"""Pydantic schemas for requests, responses and error payloads.

Units are explicit in field names:
- volumes in microlitres (uL)
- marker concentration in ng/uL
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class SampleSpec(BaseModel):
    """An initial sample (tube) declared by the client.

    A sample with ``initial_volume_ul == 0`` is an empty receiving tube;
    its marker concentration is irrelevant until it receives volume.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=64, description="Client-defined sample identifier")
    initial_volume_ul: float = Field(ge=0, allow_inf_nan=False, description="Initial volume in uL")
    marker_concentration_ng_per_ul: float = Field(
        ge=0, allow_inf_nan=False, description="Marker concentration in ng/uL"
    )


class TransferSpec(BaseModel):
    """A single liquid transfer from one sample to another."""

    model_config = ConfigDict(str_strip_whitespace=True)

    time: datetime = Field(description="When the transfer happened (ISO 8601); naive values are treated as UTC")
    source: str = Field(min_length=1, max_length=64, description="Source sample id")
    destination: str = Field(min_length=1, max_length=64, description="Destination sample id")
    volume_ul: float = Field(gt=0, allow_inf_nan=False, description="Transferred volume in uL")

    @field_validator("time")
    @classmethod
    def _naive_means_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @model_validator(mode="after")
    def _source_differs_from_destination(self) -> "TransferSpec":
        if self.source == self.destination:
            raise ValueError("source and destination must be different samples")
        return self


class SimulationRequest(BaseModel):
    """Payload for a single mixing simulation run."""

    name: str | None = Field(default=None, max_length=200, description="Optional human-readable label")
    samples: list[SampleSpec] = Field(min_length=1, max_length=1000)
    transfers: list[TransferSpec] = Field(default_factory=list, max_length=10000)

    @model_validator(mode="after")
    def _unique_sample_ids(self) -> "SimulationRequest":
        ids = [s.id for s in self.samples]
        if len(ids) != len(set(ids)):
            raise ValueError("sample ids must be unique")
        return self


# ---------------------------------------------------------------------------
# Result schemas
# ---------------------------------------------------------------------------


class SourceContribution(BaseModel):
    """How much of a sample's volume originates from a given initial sample."""

    source_id: str
    volume_ul: float
    fraction: float = Field(description="Share of the sample's current volume (0..1)")


class SampleState(BaseModel):
    """State of one sample at a point in time."""

    id: str
    volume_ul: float
    marker_concentration_ng_per_ul: float | None = Field(
        description="Volume-weighted marker concentration; null when the sample is empty"
    )
    sources: list[SourceContribution]


class Snapshot(BaseModel):
    """State of all samples at a point in time, with conservation totals."""

    total_volume_ul: float
    total_marker_ng: float
    samples: list[SampleState]


class AppliedTransfer(BaseModel):
    index: int = Field(description="Zero-based position of the transfer in the request")
    time: datetime
    source: str
    destination: str
    volume_ul: float


class StepResult(BaseModel):
    transfer: AppliedTransfer
    snapshot: Snapshot = Field(description="State of all samples right after this transfer")


class SimulationResult(BaseModel):
    initial: Snapshot = Field(description="State before any transfer")
    steps: list[StepResult]
    final: Snapshot = Field(description="State after the last transfer")


# ---------------------------------------------------------------------------
# Error schemas
# ---------------------------------------------------------------------------


class Issue(BaseModel):
    """A single, precisely located domain-validation problem."""

    code: Literal["UNKNOWN_SAMPLE", "TIME_OUT_OF_ORDER", "INSUFFICIENT_VOLUME"]
    message: str
    transfer_index: int | None = Field(default=None, description="Zero-based index of the offending transfer")
    field: str | None = Field(default=None, description="Offending field within the transfer")
    context: dict[str, Any] = Field(default_factory=dict)


class SimulationErrorBody(BaseModel):
    """Body returned with HTTP 422 when domain validation fails."""

    detail: str
    run_id: str = Field(description="Id of the persisted failed run; query it via GET /api/v1/simulations/{run_id}")
    errors: list[Issue]


# ---------------------------------------------------------------------------
# Persistence / query schemas
# ---------------------------------------------------------------------------


class SimulationRecord(BaseModel):
    """A stored simulation run (completed or failed)."""

    run_id: str
    status: Literal["completed", "failed"]
    name: str | None
    created_at: datetime
    request: SimulationRequest
    result: SimulationResult | None
    errors: list[Issue] | None


class RunSummary(BaseModel):
    run_id: str
    status: Literal["completed", "failed"]
    name: str | None
    created_at: datetime
    sample_count: int
    transfer_count: int


class RunList(BaseModel):
    items: list[RunSummary]
    total: int
    limit: int
    offset: int
