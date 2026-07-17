"""Job persistence and live progress for the web app."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config import ROOT

JOBS_DIR = ROOT / "outputs" / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    kind: str  # match | sitemap
    status: str = "queued"  # queued|running|done|error
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    message: str = ""
    progress: float = 0.0  # 0-100
    current_row: int | None = None
    total_rows: int = 0
    counters: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    error: str | None = None
    result_path: str | None = None

    def to_public(self) -> dict[str, Any]:
        d = asdict(self)
        # Keep last N events for clients
        d["events"] = self.events[-80:]
        return d


class JobStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._load_index()

    def _index_path(self) -> Path:
        return JOBS_DIR / "index.json"

    def _load_index(self) -> None:
        path = self._index_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data.get("jobs", []):
                job = Job(**{k: item[k] for k in item if k in Job.__dataclass_fields__})
                self._jobs[job.id] = job
        except Exception:
            pass

    def _save_index(self) -> None:
        jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)[:100]
        payload = {"jobs": [asdict(j) for j in jobs]}
        # trim events in persisted copy
        for j in payload["jobs"]:
            j["events"] = j.get("events", [])[-20:]
        self._index_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def create(self, kind: str, meta: dict | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, meta=meta or {})
        with self._lock:
            self._jobs[job.id] = job
            self._save_index()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 50) -> list[Job]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return jobs[:limit]

    def update(self, job_id: str, **kwargs) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            for k, v in kwargs.items():
                if hasattr(job, k):
                    setattr(job, k, v)
            job.updated_at = _now()
            self._save_index()
            return job

    def emit(self, job_id: str, message: str, **kwargs) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            event = {"ts": _now(), "message": message, **kwargs}
            job.events.append(event)
            job.message = message
            job.updated_at = _now()
            for k, v in kwargs.items():
                if k in {"progress", "current_row", "total_rows", "status", "counters"} and hasattr(job, k):
                    setattr(job, k, v)
            # Don't rewrite full index on every row — throttle
            if kwargs.get("status") in {"done", "error", "running"} or kwargs.get("progress", 0) % 10 < 0.01:
                self._save_index()


store = JobStore()
