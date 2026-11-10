"""Read-only presentation helpers for the HushMeet processing queue."""

from __future__ import annotations

import fcntl
import json
import re
from pathlib import Path
from typing import Any


QUEUE_PATH = Path.home() / ".local/state/hushmeet/process-queue.json"
LOCK_PATH = Path.home() / ".local/state/hushmeet/process-queue.lock"
UNFINISHED_STATUSES = {"pending", "running", "failed"}
ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_][0-?]*[ -/]*[@-~])")
LEGACY_RECORDING_NAME = re.compile(
    r"^(?P<title>.+) — (?P<date>\d{8})_(?P<time>\d{4})\d{2}$"
)
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def clean_display_text(value: object) -> str:
    """Remove terminal formatting and other non-printing characters for GTK."""
    text = ANSI_ESCAPE.sub("", str(value))
    lines = []
    for line in text.splitlines():
        printable = "".join(character for character in line if character.isprintable())
        normalized = " ".join(printable.split())
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def _read_queue() -> dict[str, Any]:
    """Read a stable queue snapshot without repairing or changing it."""
    if not QUEUE_PATH.exists():
        return {"jobs": []}
    try:
        if not LOCK_PATH.exists():
            return json.loads(QUEUE_PATH.read_text())
        with LOCK_PATH.open("r") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            try:
                return json.loads(QUEUE_PATH.read_text())
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except (OSError, json.JSONDecodeError):
        return {"jobs": []}


def recording_name(job: dict[str, Any]) -> str:
    audio_file = job.get("audio_file")
    if isinstance(audio_file, str) and audio_file:
        name = clean_display_text(Path(audio_file).parent.name or Path(audio_file).stem)
        legacy = LEGACY_RECORDING_NAME.match(name)
        if legacy:
            date = legacy["date"]
            timestamp = f"{date[:4]}{MONTHS[int(date[4:6]) - 1]}{date[6:]}-{legacy['time']}"
            return f"{timestamp} — {legacy['title']}"
        return name
    return "Unknown recording"


def _timestamp(job: dict[str, Any], field: str) -> str:
    value = job.get(field) or job.get("updated_at") or job.get("created_at") or ""
    return value if isinstance(value, str) else ""


def processing_status() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return all unfinished jobs and the ten most recently completed jobs."""
    jobs = _read_queue().get("jobs", [])
    if not isinstance(jobs, list):
        return [], []
    unfinished = [job for job in jobs if isinstance(job, dict) and job.get("status") in UNFINISHED_STATUSES]
    completed = [job for job in jobs if isinstance(job, dict) and job.get("status") == "succeeded"]
    unfinished.sort(key=lambda job: _timestamp(job, "created_at"))
    completed.sort(key=lambda job: _timestamp(job, "finished_at"), reverse=True)
    return unfinished, completed[:10]


def job_detail(job: dict[str, Any]) -> str:
    """Render concise category, status, and processing time for the status panel."""
    category = str(job.get("category") or "SKY")
    status = str(job.get("status") or "unknown")
    timestamp = _timestamp(job, "finished_at" if status == "succeeded" else "updated_at")
    detail = f"{category} · {status}"
    if timestamp:
        detail += f" · {timestamp.replace('T', ' ')[:16]}"
    if status == "failed" and job.get("last_error"):
        error = clean_display_text(job["last_error"])
        if error:
            detail += f"\n{error[:320]}"
    return detail
