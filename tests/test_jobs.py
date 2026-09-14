"""Job cancel and stall detector."""

from __future__ import annotations

import time

from mcp_server import jobs


def test_cancel_job_sets_event_and_status(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)

    def fn(job: jobs.Job) -> dict:
        ev = jobs.cancel_event_for(job.id)
        for _ in range(50):
            if ev.is_set():
                return {"processed": 1, "cancelled": True}
            time.sleep(0.02)
        return {"processed": 0}

    job = jobs.start_job("enrich_waterfall", fn)
    time.sleep(0.05)
    jobs.cancel_job(job.id)
    deadline = time.time() + 2
    while time.time() < deadline:
        current = jobs.get_job(job.id)
        if current.status in ("cancelled", "completed"):
            break
        time.sleep(0.05)
    current = jobs.get_job(job.id)
    assert current.status == "cancelled"
    assert jobs.cancel_event_for(job.id).is_set()


def test_stall_when_requests_made_flat(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    monkeypatch.setenv("STALL_SECONDS", "0.15")

    def fn(job: jobs.Job) -> dict:
        jobs.update_job_progress(job.id, {"requests_made": 3, "active_tier": "getleads"})
        ev = jobs.cancel_event_for(job.id)
        for _ in range(80):
            if ev.is_set():
                return dict(job.result or {})
            time.sleep(0.02)
        return {"requests_made": 3}

    job = jobs.start_job("enrich_waterfall", fn)
    deadline = time.time() + 3
    while time.time() < deadline:
        current = jobs.get_job(job.id)
        if current.status == "stalled":
            break
        time.sleep(0.05)
    current = jobs.get_job(job.id)
    assert current.status == "stalled"
    assert "getleads" in (current.error or "")
