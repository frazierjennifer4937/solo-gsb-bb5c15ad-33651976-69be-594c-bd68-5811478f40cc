"""FastAPI application: routes for running and querying mixing simulations."""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from .engine import SimulationError, simulate
from .models import (
    RunList,
    SimulationErrorBody,
    SimulationRecord,
    SimulationRequest,
)
from .storage import RunStore

DEFAULT_DB_PATH = "data/labtrack.db"


def _db_path() -> str:
    return os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = RunStore(_db_path())
    yield


app = FastAPI(
    title="Lab Sample Mixing Tracker",
    version="1.0.0",
    description=(
        "Tracks sample pooling/mixing: given initial volumes and marker "
        "concentrations plus time-ordered transfers, computes the mixed "
        "concentration and per-source proportions after every transfer "
        "under volume conservation."
    ),
    lifespan=lifespan,
)


def _get_store(request: Request) -> RunStore:
    return request.app.state.store


def _build_record(
    *,
    run_id: str,
    status: str,
    request: SimulationRequest,
    created_at: datetime,
    result: dict | None,
    errors: list[dict] | None,
) -> dict:
    return {
        "run_id": run_id,
        "status": status,
        "name": request.name,
        "created_at": created_at.isoformat(),
        "request": request.model_dump(mode="json"),
        "result": result,
        "errors": errors,
    }


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {
        "service": "lab-sample-mixing-tracker",
        "version": app.version,
        "docs": "/docs",
        "endpoints": {
            "run_simulation": "POST /api/v1/simulations",
            "get_simulation": "GET /api/v1/simulations/{run_id}",
            "list_simulations": "GET /api/v1/simulations",
            "health": "GET /health",
        },
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/v1/simulations",
    status_code=201,
    response_model=SimulationRecord,
    responses={422: {"model": SimulationErrorBody, "description": "Domain validation failed"}},
    summary="Run a single mixing simulation",
)
def create_simulation(payload: SimulationRequest, request: Request):
    """Validate and simulate the transfers, persist the run, and return it.

    Domain violations (unknown sample, time out of order, insufficient
    volume) are persisted as a failed run and reported with HTTP 422 and a
    precisely located error list.
    """
    store = _get_store(request)
    run_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc)

    try:
        result = simulate(payload)
    except SimulationError as exc:
        record = _build_record(
            run_id=run_id,
            status="failed",
            request=payload,
            created_at=created_at,
            result=None,
            errors=[issue.model_dump(mode="json") for issue in exc.issues],
        )
        store.save_run(record)
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder(
                SimulationErrorBody(
                    detail="Simulation failed domain validation; see 'errors' for located issues",
                    run_id=run_id,
                    errors=exc.issues,
                )
            ),
        )

    record = _build_record(
        run_id=run_id,
        status="completed",
        request=payload,
        created_at=created_at,
        result=result.model_dump(mode="json"),
        errors=None,
    )
    store.save_run(record)
    return record


@app.get(
    "/api/v1/simulations",
    response_model=RunList,
    summary="List stored simulation runs (newest first)",
)
def list_simulations(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    store = _get_store(request)
    items, total = store.list_runs(limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get(
    "/api/v1/simulations/{run_id}",
    response_model=SimulationRecord,
    responses={404: {"description": "Run not found"}},
    summary="Query a stored simulation result by id",
)
def get_simulation(run_id: str, request: Request) -> dict:
    store = _get_store(request)
    record = store.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"simulation run '{run_id}' not found")
    return record
