#!/usr/bin/env python3
"""Durable processing queue for HushMeet recordings."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile


ROOT = Path(__file__).resolve().parents[1]
MEETINGS_ROOT = Path("/home/ephy/dev/Dewiz/Meetings")
QUEUE_PATH = Path.home() / ".local/state/hushmeet/process-queue.json"
LOCK_PATH = Path.home() / ".local/state/hushmeet/process-queue.lock"
CATEGORY_DIRS = {
    "sky": MEETINGS_ROOT / "SKY",
    "dsolver": MEETINGS_ROOT / "dSolver",
    "dewiz": MEETINGS_ROOT / "Dewiz Command Room",
    "board": MEETINGS_ROOT / "Board Meetings",
}


class QueueBlocked(RuntimeError):
    """A global external dependency is unavailable; leave remaining jobs queued."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_queue() -> dict:
    if not QUEUE_PATH.exists():
        return {"version": 1, "jobs": []}
    try:
        data = json.loads(QUEUE_PATH.read_text())
    except json.JSONDecodeError:
        broken = QUEUE_PATH.with_suffix(f".broken-{int(time.time())}.json")
        QUEUE_PATH.rename(broken)
        print(f"Moved corrupt queue to {broken}", file=sys.stderr)
        return {"version": 1, "jobs": []}
    data.setdefault("version", 1)
    data.setdefault("jobs", [])
    return data


def save_queue(data: dict) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=QUEUE_PATH.parent, delete=False) as tmp:
        json.dump(data, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(QUEUE_PATH)


def with_lock(fn):
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return fn()


def infer_category(path: Path) -> str:
    resolved = path.resolve()
    for category, directory in CATEGORY_DIRS.items():
        try:
            resolved.relative_to(directory)
            return category
        except ValueError:
            continue
    return os.environ.get("HUSHNOTE_CATEGORY", "sky")


def base_for_audio(audio: Path) -> Path:
    stem = audio.stem.removesuffix("_trimmed")
    return audio.with_name(stem)


def transcript_for_audio(audio: Path) -> Path | None:
    candidates = [
        base_for_audio(audio).with_suffix(".md"),
        audio.with_suffix(".md"),
    ]
    return next((path for path in candidates if path.exists()), None)


def is_meeting_note(path: Path, base: Path) -> bool:
    if path.name in {f"{base.name}.md", f"{base.name}_summary.md"}:
        return False
    try:
        head = path.read_text(errors="ignore")[:300]
    except OSError:
        return False
    return "type: meeting" in head and "status: processed" in head


def has_meeting_note(audio: Path) -> bool:
    base = base_for_audio(audio)
    return any(is_meeting_note(path, base) for path in audio.parent.glob("*.md"))


def find_audio(meeting_dir: Path) -> Path | None:
    candidates = sorted(
        [*meeting_dir.glob("*_trimmed.mp3"), *meeting_dir.glob("*.mp3"), *meeting_dir.glob("*.wav")],
        key=lambda path: (path.name.endswith("_trimmed.mp3"), path.stat().st_mtime),
        reverse=True,
    )
    return candidates[0] if candidates else None


def normalize_audio_file(audio_file: str) -> str:
    audio = Path(audio_file).resolve()
    if audio.exists():
        return str(audio)
    replacement = find_audio(audio.parent)
    if not replacement:
        raise FileNotFoundError(audio)

    def update() -> str:
        data = load_queue()
        for job in data["jobs"]:
            if job.get("audio_file") == audio_file:
                job["audio_file"] = str(replacement.resolve())
                job["last_error"] = f"Repaired missing audio path from {audio.name} to {replacement.name}"
                job["updated_at"] = now()
                break
        save_queue(data)
        return str(replacement.resolve())

    return with_lock(update)


def upsert_job(audio: Path, category: str | None = None) -> bool:
    audio = audio.resolve()
    if not audio.exists():
        raise FileNotFoundError(audio)
    if audio.suffix.lower() not in {".wav", ".mp3", ".m4a", ".ogg", ".webm", ".flac"}:
        raise ValueError(f"Unsupported audio file: {audio}")

    def update() -> bool:
        data = load_queue()
        audio_str = str(audio)
        for job in data["jobs"]:
            if job.get("audio_file") == audio_str:
                if job.get("status") == "succeeded" and not has_meeting_note(audio):
                    job["status"] = "pending"
                    job["updated_at"] = now()
                    save_queue(data)
                    return True
                return False
        data["jobs"].append(
            {
                "audio_file": audio_str,
                "category": category or infer_category(audio),
                "status": "pending",
                "attempts": 0,
                "created_at": now(),
                "updated_at": now(),
            }
        )
        save_queue(data)
        return True

    return with_lock(update)


def seed_missing() -> int:
    added = 0
    for category, category_dir in CATEGORY_DIRS.items():
        if not category_dir.exists():
            continue
        for meeting_dir in sorted(path for path in category_dir.glob("[0-9]" * 8 + "/*") if path.is_dir()):
            audio = find_audio(meeting_dir)
            if audio and not has_meeting_note(audio):
                if upsert_job(audio, category):
                    print(f"queued {audio}")
                    added += 1
    return added


def update_job(audio_file: str, **fields) -> None:
    def update() -> None:
        data = load_queue()
        for job in data["jobs"]:
            if job.get("audio_file") == audio_file:
                job.update(fields)
                job["updated_at"] = now()
                break
        save_queue(data)

    with_lock(update)


def dismiss_job(audio: Path) -> bool:
    """Hide an intentionally skipped recording without deleting its audio."""
    audio_file = str(audio.resolve())

    def update() -> bool:
        data = load_queue()
        for job in data["jobs"]:
            if job.get("audio_file") == audio_file:
                job.update(
                    status="ignored",
                    last_error="Dismissed by user; audio retained without processing.",
                    dismissed_at=now(),
                )
                job["updated_at"] = now()
                save_queue(data)
                return True
        return False

    return with_lock(update)


def pending_jobs() -> list[dict]:
    def read() -> list[dict]:
        data = load_queue()
        return [
            job
            for job in data["jobs"]
            if job.get("status") in {"pending", "failed", "running"}
        ]

    return with_lock(read)


def summarize(transcript: Path, category: str) -> Path:
    env = os.environ.copy()
    env["HUSHNOTE_CATEGORY"] = category
    env["HUSHNOTE_TRANSCRIBE_FORMAT"] = "md"
    run_checked([str(ROOT / "hushnote"), "summarize", str(transcript)], env=env)
    summary = transcript.with_name(f"{transcript.stem.removesuffix('_trimmed')}_summary.md")
    if not summary.exists():
        raise RuntimeError(f"summary was not created: {summary}")
    return summary


def transcribe(audio: Path, category: str) -> Path:
    env = os.environ.copy()
    env["HUSHNOTE_CATEGORY"] = category
    env["HUSHNOTE_TRANSCRIBE_FORMAT"] = "md"
    run_checked([str(ROOT / "hushnote"), "transcribe", str(audio), "-f", "md"], env=env)
    transcript = transcript_for_audio(audio)
    if not transcript:
        raise RuntimeError(f"transcript was not created for {audio}")
    return transcript


def run_checked(command: list[str], env: dict) -> None:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode == 0:
        return

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    message = output.strip() or f"{command} exited {result.returncode}"
    if "insufficient_quota" in output or "exceeded your current quota" in output:
        raise QueueBlocked(message)
    raise RuntimeError(message)


def run_hook(summary: Path, category: str) -> Path:
    env = os.environ.copy()
    env["HUSHNOTE_CATEGORY"] = category
    hook = ROOT / "scripts/obsidian-meeting-note.py"
    result = subprocess.run(
        [str(hook), str(summary)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    note_path = Path(result.stdout.strip().splitlines()[-1]).resolve()
    marker = summary.with_name(f"{summary.name.removesuffix('_summary.md')}.hook_done")
    marker.touch()
    return note_path


def process_job(job: dict) -> None:
    audio = Path(job["audio_file"]).resolve()

    category = job.get("category") or infer_category(audio)
    base = base_for_audio(audio)
    transcript = transcript_for_audio(audio)
    summary = base.with_name(f"{base.name}_summary.md")

    if has_meeting_note(audio):
        return

    env = os.environ.copy()
    env["HUSHNOTE_CATEGORY"] = category
    env["HUSHNOTE_TRANSCRIBE_FORMAT"] = "md"

    if not summary.exists():
        if transcript:
            summary = summarize(transcript, category)
        else:
            transcript = transcribe(audio, category)
            summary = summarize(transcript, category)
            if not summary.exists():
                raise RuntimeError(f"processing did not create summary: {summary}")

    if not has_meeting_note(audio):
        run_hook(summary, category)
    if not has_meeting_note(audio):
        raise RuntimeError(f"post-summary hook did not create meeting note for {audio}")


def process_queue(limit: int | None) -> int:
    discovered = seed_missing()
    if discovered:
        print(f"queued {discovered} missing meeting(s)")

    processed = 0
    failures = 0
    for job in pending_jobs():
        if limit is not None and processed >= limit:
            break
        audio_file = normalize_audio_file(job["audio_file"])
        job["audio_file"] = audio_file
        attempts = int(job.get("attempts", 0)) + 1
        update_job(audio_file, status="running", attempts=attempts, started_at=now(), last_error="")
        try:
            process_job(job)
        except QueueBlocked as exc:
            failures += 1
            update_job(audio_file, status="failed", last_error=str(exc), failed_at=now())
            print(f"blocked {audio_file}: {exc}", file=sys.stderr)
            print("Stopping this queue run; remaining jobs will retry later.", file=sys.stderr)
            break
        except Exception as exc:
            failures += 1
            update_job(audio_file, status="failed", last_error=str(exc), failed_at=now())
            print(f"failed {audio_file}: {exc}", file=sys.stderr)
        else:
            processed += 1
            update_job(audio_file, status="succeeded", finished_at=now(), last_error="")
            print(f"processed {audio_file}")
    return 1 if failures else 0


def list_queue() -> int:
    data = with_lock(load_queue)
    for job in data["jobs"]:
        print(
            f"{job.get('status','unknown'):9} attempts={job.get('attempts',0)} "
            f"{job.get('category','?'):7} {job.get('audio_file')}"
        )
        if job.get("last_error"):
            print(f"  last_error: {job['last_error']}")
    return 0


def repair_queue() -> int:
    repaired = 0

    def update() -> int:
        nonlocal repaired
        data = load_queue()
        for job in data["jobs"]:
            audio = Path(job.get("audio_file", "")).resolve()
            if audio.exists():
                continue
            replacement = find_audio(audio.parent)
            if not replacement:
                continue
            job["audio_file"] = str(replacement.resolve())
            job["last_error"] = f"Repaired missing audio path from {audio.name} to {replacement.name}"
            job["updated_at"] = now()
            repaired += 1
        save_queue(data)
        return repaired

    return with_lock(update)


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the HushMeet processing queue")
    sub = parser.add_subparsers(dest="command", required=True)

    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("audio_file")
    enqueue.add_argument("--category")

    sub.add_parser("seed-missing")

    process = sub.add_parser("process")
    process.add_argument("--limit", type=int)

    dismiss = sub.add_parser("dismiss")
    dismiss.add_argument("audio_file")

    sub.add_parser("list")
    sub.add_parser("repair")

    args = parser.parse_args()
    if args.command == "enqueue":
        added = upsert_job(Path(args.audio_file), args.category)
        print(("queued" if added else "already queued") + f" {Path(args.audio_file).resolve()}")
        return 0
    if args.command == "seed-missing":
        added = seed_missing()
        print(f"queued {added} missing meeting(s)")
        return 0
    if args.command == "process":
        return process_queue(args.limit)
    if args.command == "dismiss":
        dismissed = dismiss_job(Path(args.audio_file))
        print("dismissed" if dismissed else "not queued")
        return 0 if dismissed else 1
    if args.command == "list":
        return list_queue()
    if args.command == "repair":
        repaired = repair_queue()
        print(f"repaired {repaired} job(s)")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
