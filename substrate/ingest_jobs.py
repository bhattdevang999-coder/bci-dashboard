"""Async ingest jobs for the catalog (and future ingest pipelines).

Render's HTTP layer kills requests at ~30s. Our XLSX ingest of 38k ASINs
takes 30-45s on prod Postgres, which exceeds that limit. This module
splits ingest into:

  1) Enqueue: client POSTs file → we save it to disk, INSERT a job row
     with status='queued', spawn a daemon thread, return job_id.
  2) Poll: client GETs /ingest-status/<job_id> every ~2s until status
     is 'done' or 'failed'.

The worker is a Python daemon thread, not Celery/RQ. We're single-instance
on Render — a thread is the right tool. If we ever need multi-worker, we
swap this for a queue-backed worker without changing the API.

Idempotent at the DB layer (job_id is a UUID). At-most-once delivery.
A crashed worker leaves the job in 'running' forever — a stale-job sweep
on /api/atlas/catalog/ingest-status auto-flips jobs older than 5 min to
'failed' so the UI doesn't spin forever.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from substrate.db import get_pool

logger = logging.getLogger("atlas.substrate.ingest_jobs")

_STALE_AFTER_SECONDS = 5 * 60  # 5 minutes


def create_job(
    workspace_id: str,
    filepath: Optional[str] = None,
    *,
    filename: Optional[str] = None,
    file_size_bytes: Optional[int] = None,
    preview_only: bool = False,
    created_by: str = "devang",
    job_type: str = "catalog_ingest",
) -> Optional[str]:
    """Insert a queued job row. Returns job_id or None on failure."""
    pool = get_pool()
    if pool is None:
        return None
    job_id = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ingest_jobs (
                        job_id, workspace_id, job_type, filepath, filename,
                        file_size_bytes, preview_only, status, created_by
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, 'queued', %s
                    )
                    """,
                    (job_id, workspace_id, job_type, filepath, filename,
                     file_size_bytes, preview_only, created_by),
                )
            conn.commit()
        return job_id
    except Exception as exc:
        logger.warning("create_job failed: %s", exc)
        return None


def _set_status(
    job_id: str,
    status: str,
    *,
    progress_pct: Optional[int] = None,
    progress_message: Optional[str] = None,
    result: Optional[dict] = None,
    error: Optional[str] = None,
    mark_started: bool = False,
    mark_completed: bool = False,
) -> bool:
    pool = get_pool()
    if pool is None:
        return False
    sets = ["status = %s"]
    params: list[Any] = [status]
    if progress_pct is not None:
        sets.append("progress_pct = %s")
        params.append(progress_pct)
    if progress_message is not None:
        sets.append("progress_message = %s")
        params.append(progress_message)
    if result is not None:
        sets.append("result_json = %s::jsonb")
        params.append(json.dumps(result, default=str))
    if error is not None:
        sets.append("error = %s")
        params.append(error[:2000])
    if mark_started:
        sets.append("started_at = NOW()")
    if mark_completed:
        sets.append("completed_at = NOW()")
        sets.append(
            "duration_seconds = "
            "EXTRACT(EPOCH FROM (NOW() - started_at))"
        )
    params.append(job_id)
    sql = f"UPDATE ingest_jobs SET {', '.join(sets)} WHERE job_id = %s"
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
            conn.commit()
        return True
    except Exception as exc:
        logger.warning("_set_status failed: %s", exc)
        return False


def get_job(job_id: str) -> Optional[dict]:
    """Fetch one job row. Sweeps stale 'running' jobs first as a side effect."""
    _sweep_stale_running()
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT job_id, workspace_id, job_type, filename,
                           file_size_bytes, preview_only,
                           status, progress_pct, progress_message,
                           result_json, error,
                           created_at, started_at, completed_at,
                           duration_seconds, created_by
                    FROM ingest_jobs WHERE job_id = %s
                    """,
                    (job_id,),
                )
                r = cur.fetchone()
                if not r:
                    return None
                return {
                    "job_id": r[0],
                    "workspace_id": r[1],
                    "job_type": r[2],
                    "filename": r[3],
                    "file_size_bytes": r[4],
                    "preview_only": r[5],
                    "status": r[6],
                    "progress_pct": r[7],
                    "progress_message": r[8],
                    "result_json": r[9],
                    "error": r[10],
                    "created_at": r[11].isoformat() if r[11] else None,
                    "started_at": r[12].isoformat() if r[12] else None,
                    "completed_at": r[13].isoformat() if r[13] else None,
                    "duration_seconds":
                        float(r[14]) if r[14] is not None else None,
                    "created_by": r[15],
                }
    except Exception as exc:
        logger.warning("get_job failed: %s", exc)
        return None


def _sweep_stale_running() -> None:
    """Flip 'running' jobs older than 5min to 'failed'. Catches crashes."""
    pool = get_pool()
    if pool is None:
        return
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ingest_jobs
                       SET status = 'failed',
                           error = COALESCE(error, '') ||
                                   ' [auto-failed: worker stalled > 5min]',
                           completed_at = NOW()
                     WHERE status IN ('queued', 'running')
                       AND created_at < NOW() - INTERVAL '5 minutes'
                    """,
                )
            conn.commit()
    except Exception as exc:
        logger.warning("_sweep_stale_running failed: %s", exc)


def list_recent_jobs(
    workspace_id: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Recent jobs, newest first. Used by /api/atlas/catalog/ingest-history."""
    pool = get_pool()
    if pool is None:
        return []
    where, params = [], []
    if workspace_id:
        where.append("workspace_id = %s")
        params.append(workspace_id)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    sql = f"""
        SELECT job_id, workspace_id, filename, status, progress_pct,
               created_at, completed_at, duration_seconds, error
          FROM ingest_jobs
          {where_clause}
         ORDER BY created_at DESC
         LIMIT %s
    """
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
        return [
            {
                "job_id": r[0],
                "workspace_id": r[1],
                "filename": r[2],
                "status": r[3],
                "progress_pct": r[4],
                "created_at": r[5].isoformat() if r[5] else None,
                "completed_at": r[6].isoformat() if r[6] else None,
                "duration_seconds":
                    float(r[7]) if r[7] is not None else None,
                "error": r[8],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("list_recent_jobs failed: %s", exc)
        return []


# ─── Worker ───────────────────────────────────────────────────────────


def _run_catalog_ingest(job_id: str) -> None:
    """Worker body — runs in a daemon thread."""
    # Look up the job
    job = get_job(job_id)
    if not job:
        logger.warning("worker: job %s not found", job_id)
        return
    if job["status"] in ("done", "failed"):
        return  # already terminal

    _set_status(job_id, "running",
                progress_pct=5,
                progress_message="Reading workbook…",
                mark_started=True)

    try:
        # Lazy import — keeps module load fast.
        from substrate.catalog_ingest import ingest_workbook
        # Re-fetch filepath (avoid stale reads)
        pool = get_pool()
        filepath = None
        preview_only = False
        if pool is not None:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT filepath, preview_only, workspace_id, created_by "
                        "FROM ingest_jobs WHERE job_id = %s",
                        (job_id,),
                    )
                    r = cur.fetchone()
                    if r:
                        filepath, preview_only, workspace_id, created_by = r
        if not filepath or not os.path.exists(filepath):
            raise RuntimeError(f"file missing at worker time: {filepath}")

        _set_status(job_id, "running",
                    progress_pct=25,
                    progress_message="Parsing rows + classifying cohorts…")

        def _on_progress(pct: int, msg: str) -> None:
            # Best-effort progress update. Don't let a DB hiccup kill the
            # worker — _set_status already swallows its own errors.
            _set_status(job_id, "running",
                        progress_pct=pct, progress_message=msg)

        result = ingest_workbook(
            filepath, workspace_id,
            write_substrate=not preview_only,
            ingested_by=created_by or "devang",
            progress_cb=_on_progress,
        )

        _set_status(job_id, "done",
                    progress_pct=100,
                    progress_message="Substrate written.",
                    result=result,
                    mark_completed=True)
    except Exception as exc:
        tb = traceback.format_exc()
        logger.warning("ingest worker failed for %s: %s\n%s",
                       job_id, exc, tb)
        _set_status(job_id, "failed",
                    error=f"{exc}",
                    mark_completed=True)


def _run_catalog_audit(job_id: str) -> None:
    """Worker body for job_type='catalog_audit'. Runs the audit engine."""
    job = get_job(job_id)
    if not job:
        logger.warning("audit worker: job %s not found", job_id)
        return
    if job["status"] in ("done", "failed"):
        return

    _set_status(job_id, "running", progress_pct=5,
                progress_message="Resolving rule set…", mark_started=True)

    try:
        from substrate.catalog_audit_engine import run_audit
        # The audit engine is fast (<20s on 38k Roxy) so we don't need
        # fine-grained progress — just mark running until done.
        _set_status(job_id, "running", progress_pct=30,
                    progress_message="Evaluating rules…")
        result = run_audit(
            job["workspace_id"],
            run_id=job_id,  # reuse the job_id as the audit_run_id
            dry_run=False,
        )
        _set_status(job_id, "done", progress_pct=100,
                    progress_message=(
                        f"{result['total_findings']:,} findings written."
                    ),
                    result=result, mark_completed=True)
    except Exception as exc:
        tb = traceback.format_exc()
        logger.warning("audit worker failed for %s: %s\n%s",
                       job_id, exc, tb)
        _set_status(job_id, "failed", error=f"{exc}", mark_completed=True)


def spawn_worker(job_id: str) -> None:
    """Spawn a daemon thread to run the job. Returns immediately.

    Worker target is chosen by job_type. Default is catalog_ingest for
    backwards compatibility.
    """
    job = get_job(job_id)
    job_type = (job or {}).get("job_type") or "catalog_ingest"
    target = {
        "catalog_ingest": _run_catalog_ingest,
        "catalog_audit":  _run_catalog_audit,
    }.get(job_type, _run_catalog_ingest)

    t = threading.Thread(
        target=target,
        args=(job_id,),
        name=f"{job_type}-worker-{job_id[:8]}",
        daemon=True,
    )
    t.start()


__all__ = [
    "create_job",
    "get_job",
    "list_recent_jobs",
    "spawn_worker",
]
