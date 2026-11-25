"""Small in-process job registry for local GreenPulse day-ahead runs.

This avoids keeping a client HTTP request open during 96 MTUs of upstream
collection and optimization. Jobs are intentionally in-memory for local use;
a multi-worker deployment should replace this registry with a durable queue.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from threading import Lock
from typing import Any
from uuid import uuid4

from services.api.day_ahead_forecast import run_both_day_ahead_models

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="greenpulse-day-ahead")
_LOCK = Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_MAX_RETAINED_JOBS = 40


def _update(job_id: str, **fields: Any) -> None:
    with _LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(fields)
            _JOBS[job_id]["updated_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")



def _run_job(job_id: str, arguments: dict[str, Any]) -> None:
    _update(job_id, status="RUNNING", stage="COLLECTING_INPUTS")

    def progress(stage: str, completed: int | None = None, total: int | None = None) -> None:
        _update(job_id, stage=stage, completed_intervals=completed, expected_intervals=total)

    try:
        result = run_both_day_ahead_models(**arguments, progress_callback=progress, persist_snapshot=True)
        final_status = result.get("status", "COMPLETED")
        _update(job_id, status= final_status,stage= final_status,result= result, error=None)
    except Exception as error:  # Keep the exception visible to the client without killing the worker.
        _update(job_id, status="FAILED", stage="FAILED", error=f"{type(error).__name__}: {error}")


def submit_day_ahead_job(
    delivery_day: date,
    *,
    forecast_origin_utc: datetime | None,
    daily_hydro_energy_budget_mwh: dict[str, float] | None = None,
    max_workers: int = 4,
    mode: str = "FORECAST",
    scenario_id: str = "normal_base",
    refresh_inputs: bool = False,
) -> dict[str, Any]:
    job_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    arguments = {
        "delivery_day": delivery_day,
        "forecast_origin_utc": forecast_origin_utc,
        "daily_hydro_energy_budget_mwh": dict(daily_hydro_energy_budget_mwh or {}),
        "max_workers": max_workers,
        "mode": mode,
        "scenario_id": scenario_id,
        "refresh_inputs": refresh_inputs,
    }
    with _LOCK:
        _JOBS[job_id] = {
            "job_id": job_id,
            "status": "QUEUED",
            "stage": "QUEUED",
            "delivery_day_local": delivery_day.isoformat(),
            "created_at_utc": now,
            "updated_at_utc": now,
            "completed_intervals": 0,
            "expected_intervals": None,
            "mode": mode,
            "scenario_id": scenario_id,
        }
        # Keep a bounded in-memory history; do not evict active jobs.
        if len(_JOBS) > _MAX_RETAINED_JOBS:
            finished = [key for key, value in _JOBS.items() if value.get("status") not in {"QUEUED", "RUNNING"}]
            for key in finished[: max(0, len(_JOBS) - _MAX_RETAINED_JOBS)]:
                _JOBS.pop(key, None)
    _EXECUTOR.submit(_run_job, job_id, arguments)
    return get_day_ahead_job(job_id) or {"job_id": job_id, "status": "QUEUED"}


def get_day_ahead_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        value = _JOBS.get(job_id)
        return dict(value) if value is not None else None
