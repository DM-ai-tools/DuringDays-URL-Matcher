"""Persistent working copies so repeated runs on the same upload accumulate."""

from __future__ import annotations

import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

from config import ROOT

WORKING_DIR = ROOT / "outputs" / "working"
WORKING_DIR.mkdir(parents=True, exist_ok=True)

_locks_guard = threading.Lock()
_file_locks: dict[str, threading.Lock] = {}


def working_path(file_id: str) -> Path:
    return WORKING_DIR / file_id


def has_working_copy(file_id: str) -> bool:
    return working_path(file_id).exists()


@contextmanager
def file_job_lock(file_id: str):
    with _locks_guard:
        if file_id not in _file_locks:
            _file_locks[file_id] = threading.Lock()
        lock = _file_locks[file_id]
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def prepare_working(upload_path: Path, file_id: str) -> tuple[Path, bool]:
    """
    Return the workbook path to read/write for this upload.
    Creates a working copy from the upload on first use; reuses it afterward.
    """
    dest = working_path(file_id)
    if dest.exists():
        return dest, True
    shutil.copy2(upload_path, dest)
    return dest, False
