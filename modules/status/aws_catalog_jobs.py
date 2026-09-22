from __future__ import annotations

import json
import os
import threading
import time
import uuid
import re
from pathlib import Path

from services.aws_instance_types.common import acquire_lock, release_lock
from typing import Any


def _job_dir() -> str:
    # Keep jobs on local disk so any gunicorn worker can serve status polling.
    base = (
        os.getenv("PS_AWS_CATALOG_JOB_DIR")
        or os.getenv("PS_EKS_CLUSTER_VERSIONS_CACHE_DIR")
        or os.getenv("PS_EKS_INSTANCE_TYPES_CACHE_DIR")
        or os.getenv("PS_AURORA_ENGINE_VERSIONS_CACHE_DIR")
        or os.getenv("PS_AURORA_INSTANCE_CLASSES_CACHE_DIR")
        or os.getenv("PS_REDIS_NODE_TYPES_CACHE_DIR")
        or os.getenv("PS_KAFKA_CATALOG_CACHE_DIR")
        or "/tmp/praxis-cache"
    )
    return os.path.join(base.rstrip("/"), "aws-catalog-jobs")


def _job_path(job_id: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", job_id):
        raise ValueError("Invalid job ID")
    return os.path.join(_job_dir(), f"{job_id}.json")


def _write_json_atomic(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{uuid.uuid4().hex}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def get_job(job_id: str) -> dict[str, Any] | None:
    try:
        job = _read_json(_job_path(job_id))
    except ValueError:
        return None
    if job and job.get('state') in ('queued', 'running'):
        _recover_abandoned(job_id, job)
    return job


def _recover_abandoned(job_id, job):
    lease = acquire_lock(_job_path(job_id) + '.lock', blocking=False)
    if lease is not None:
        try:
            # The worker may have completed between our read and acquiring its lease.
            latest = _read_json(_job_path(job_id)) or dict(job)
            job.clear()
            job.update(latest)
            if job.get('state') in ('queued', 'running'):
                job.update(state='error', ok=False, error='Refresh worker stopped before completion',
                           finished_at_epoch=time.time())
                _write_json_atomic(_job_path(job_id), job)
        finally:
            release_lock(lease)


def _cleanup_jobs():
    active = []
    cutoff = time.time() - 7 * 24 * 3600
    for path in Path(_job_dir()).glob('*.json'):
        job = _read_json(str(path)) or {}
        if job.get('state') in ('queued', 'running'):
            _recover_abandoned(path.stem, job)
        if job.get('state') in ('queued', 'running'):
            active.append(job)
        elif (job.get('finished_at_epoch') or job.get('created_at_epoch') or 0) < cutoff:
            path.unlink(missing_ok=True)
            Path(str(path) + '.lock').unlink(missing_ok=True)
    return active



def _catalogs_for_request(catalog: str) -> list[str]:
    from services.aws_instance_types.registry import requested_catalogs
    return requested_catalogs(catalog)


def start_refresh_job(*, catalog: str, regions: list[str], engine: str) -> dict[str, Any]:
    cats = _catalogs_for_request(catalog)
    regions = sorted(set(regions))
    guard = acquire_lock(os.path.join(_job_dir(), 'jobs.lock'), blocking=True)
    if guard is None:
        return {'ok': False, 'error': 'Refresh job lock unavailable'}
    try:
        active = _cleanup_jobs()
        for job in active:
            if job.get('catalog') == (catalog or 'all') and job.get('regions') == regions and job.get('engine') == engine:
                return {'ok': True, 'job_id': job['job_id'], 'state': job['state'], 'deduplicated': True}
        if len(active) >= 2:
            return {'ok': False, 'error': 'Two refresh jobs are already active; retry when one finishes'}
        job_id = uuid.uuid4().hex
        lease = acquire_lock(_job_path(job_id) + '.lock', blocking=False)
        if lease is None:
            return {'ok': False, 'error': 'Refresh worker lock unavailable'}
        payload = {
            'job_id': job_id, 'created_at_epoch': time.time(), 'started_at_epoch': None,
            'finished_at_epoch': None, 'state': 'queued', 'ok': None,
            'catalog': catalog or 'all', 'regions': regions, 'engine': engine,
            'total_steps': len(regions) * len(cats), 'completed_steps': 0,
            'events': [], 'results': {}, 'error': None,
        }
        def run():
            try:
                _run_refresh_job(job_id, catalog or 'all', regions, engine)
            finally:
                release_lock(lease)
        try:
            _write_json_atomic(_job_path(job_id), payload)
            threading.Thread(target=run, name=f'aws-catalog-refresh:{job_id}', daemon=True).start()
        except Exception:
            release_lock(lease)
            raise
        return {'ok': True, 'job_id': job_id, 'state': 'queued'}
    finally:
        release_lock(guard)


def _append_event(job: dict[str, Any], *, level: str, message: str, region: str | None = None, catalog: str | None = None) -> None:
    ev = {
        "ts_epoch": time.time(),
        "level": level,
        "message": message,
        "region": region,
        "catalog": catalog,
    }
    events = job.get("events")
    if not isinstance(events, list):
        events = []
        job["events"] = events
    events.append(ev)


def _set_state(job: dict[str, Any], state: str) -> None:
    job["state"] = state


def _run_refresh_job(job_id: str, catalog: str, regions: list[str], engine: str) -> None:
    path = _job_path(job_id)
    job = _read_json(path) or {}

    try:
        job["started_at_epoch"] = time.time()
        _set_state(job, "running")
        _append_event(job, level="info", message="Job started")
        _write_json_atomic(path, job)

        cats = _catalogs_for_request(catalog)
        results: dict[str, Any] = job.get("results") if isinstance(job.get("results"), dict) else {}
        job["results"] = results

        completed = int(job.get("completed_steps") or 0)

        for region in regions:
            if region not in results or not isinstance(results.get(region), dict):
                results[region] = {}

            for cat in cats:
                _append_event(job, level="info", message=f"Refreshing {cat} in {region}...", region=region, catalog=cat)
                _write_json_atomic(path, job)

                from services.aws_instance_types.registry import refresh_region
                batch = refresh_region(region, catalogs=[cat], force=True,
                                       engine=engine if catalog not in ('', 'all') else '')
                res = {'ok': all(value.get('ok') for value in batch.values()), 'entries': batch}

                results[region][cat] = res
                completed += 1
                job["completed_steps"] = completed

                if (res or {}).get("ok"):
                    _append_event(job, level="info", message=f"Done {cat} in {region}", region=region, catalog=cat)
                else:
                    _append_event(job, level="warning", message=f"Failed {cat} in {region}", region=region, catalog=cat)
                _write_json_atomic(path, job)

        # Determine overall OK.
        ok_all = True
        for region in regions:
            per = results.get(region) if isinstance(results.get(region), dict) else {}
            for cat in cats:
                if not (per.get(cat) or {}).get("ok"):
                    ok_all = False
        job["ok"] = ok_all
        job["finished_at_epoch"] = time.time()
        _set_state(job, "done")
        _append_event(job, level="info", message="Job finished")
        _write_json_atomic(path, job)

    except Exception as exc:
        job = _read_json(path) or job
        job["ok"] = False
        job["error"] = str(exc)
        job["finished_at_epoch"] = time.time()
        _set_state(job, "error")
        _append_event(job, level="error", message=f"Job crashed: {exc}")
        _write_json_atomic(path, job)
