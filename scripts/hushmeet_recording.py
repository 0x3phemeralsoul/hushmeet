"""Shared recording state and launcher functions for HushMeet clients.

Both the background watcher and the desktop widget use this module.  Keeping
the process-group and state-file logic in one place ensures that either client
can accurately display or stop a recording started by the other.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = Path.home() / ".local/state/hushmeet/active.json"
CATEGORIES = [
    ("sky", "SKY"),
    ("dsolver", "dSolver"),
    ("dewiz", "Dewiz Command Room"),
    ("board", "Board Meetings"),
]


@dataclass(frozen=True)
class AudioInput:
    """An audio source that can be selected by a desktop client."""

    key: str
    label: str
    source_type: str = "both"
    source: str | None = None


def category_for_title(title: str) -> str:
    """Return the category suggested by a meeting title, falling back to SKY."""
    normalized = title.casefold()
    for category, label in CATEGORIES:
        if category in normalized or label.casefold() in normalized:
            return category
    return "sky"


def category_label(category: str) -> str:
    return next((label for value, label in CATEGORIES if value == category), "SKY")


def _process_group_is_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _remove_state() -> None:
    STATE_PATH.unlink(missing_ok=True)


def active_recording() -> dict[str, Any] | None:
    """Return an active recording, deleting a malformed or stale state file."""
    if not STATE_PATH.exists():
        return None
    try:
        data = json.loads(STATE_PATH.read_text())
        pid = int(data["pid"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        _remove_state()
        return None
    if not _process_group_is_alive(pid):
        _remove_state()
        return None
    return data


def write_active_recording(data: dict[str, Any]) -> None:
    """Atomically persist state so indicator refreshes never read partial JSON."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=STATE_PATH.parent, prefix="active.", delete=False
    ) as handle:
        json.dump(data, handle)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.chmod(0o600)
    temp_path.replace(STATE_PATH)


def recording_command(
    category: str,
    title: str,
    audio_source_type: str = "both",
    audio_source: str | None = None,
) -> list[str]:
    """Build the stable HushMeet invocation used by every recording client."""
    command = [
        str(ROOT / "scripts/hushmeet"),
        category,
        "--title",
        title,
        "--audio-source-type",
        audio_source_type,
    ]
    if audio_source:
        command.extend(["--audio-source", audio_source])
    return command


def start_recording(
    category: str,
    title: str,
    source: str,
    *,
    audio_source_type: str = "both",
    audio_source: str | None = None,
    audio_label: str | None = None,
    meet_url: str | None = None,
) -> dict[str, Any]:
    """Launch a recording in its own process group and publish its state."""
    if active_recording():
        raise RuntimeError("A HushMeet recording is already active.")
    normalized_title = title.strip()
    if not normalized_title:
        raise ValueError("A meeting name is required.")
    if category not in {value for value, _ in CATEGORIES}:
        raise ValueError("Choose a valid HushMeet category.")

    process = subprocess.Popen(
        recording_command(category, normalized_title, audio_source_type, audio_source),
        start_new_session=True,
    )
    data: dict[str, Any] = {
        "pid": process.pid,
        "title": normalized_title,
        "category": category,
        "category_label": category_label(category),
        "source": source,
        "audio_source_type": audio_source_type,
        "audio_source": audio_source,
        "audio_label": audio_label or "Default microphone + system audio",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    if meet_url:
        data["meet_url"] = meet_url.rstrip("/")
    write_active_recording(data)
    return data


def stop_recording() -> bool:
    """Signal the recorder process group and remove its active state."""
    data = active_recording()
    if not data:
        return False
    try:
        os.killpg(int(data["pid"]), signal.SIGINT)
    except ProcessLookupError:
        pass
    _remove_state()
    return True


def _source_description(source: dict[str, Any]) -> str:
    properties = source.get("properties") or {}
    return str(
        source.get("description")
        or properties.get("device.description")
        or properties.get("device.product.name")
        or source.get("name")
        or "Audio source"
    )


def available_audio_inputs() -> list[AudioInput]:
    """Return system-audio mixes using the default or each selectable microphone."""
    inputs = [
        AudioInput("both", "Default microphone + system audio", "both"),
    ]
    result = subprocess.run(
        ["pactl", "--format=json", "list", "sources"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return inputs
    try:
        sources = json.loads(result.stdout)
    except json.JSONDecodeError:
        return inputs
    if isinstance(sources, dict):
        sources = sources.get("sources", [])
    if not isinstance(sources, list):
        return inputs
    for source in sources:
        if not isinstance(source, dict) or not source.get("name"):
            continue
        source_name = str(source["name"])
        if source_name.endswith(".monitor"):
            continue
        description = _source_description(source)
        inputs.append(
            AudioInput(
                key=f"source:{source_name}",
                label=f"{description} + system audio ({source_name})",
                source_type="both",
                source=source_name,
            )
        )
    return inputs
