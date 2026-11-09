#!/usr/bin/env python3
"""Prompt to record current Google Calendar Meet events or Discord voice activity.

This watcher deliberately never records without a local GUI confirmation. Google
Calendar credentials and OAuth tokens remain in ~/.config/hushmeet and are not
stored in the repository or Obsidian vault.
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hushmeet_calendar import calendar_events, load_config
from hushmeet_recording import (
    CATEGORIES,
    active_recording,
    start_recording,
    stop_recording,
    write_active_recording,
)

DEFAULT_DISCONNECT_GRACE_SECONDS = 90


def now_events(config: dict, require_meet: bool = True) -> list[dict]:
    """Return Calendar events currently in progress for watcher decisions."""
    return calendar_events(config, require_meet=require_meet)


def event_meet_urls(event: dict) -> set[str]:
    urls = {event.get("hangoutLink", "")}
    for point in event.get("conferenceData", {}).get("entryPoints", []):
        urls.add(point.get("uri", ""))
    return {url.rstrip("/") for url in urls if url}


def discord_in_voice() -> bool:
    """Best-effort PipeWire/PulseAudio detection of a Discord communication stream."""
    result = subprocess.run(["pactl", "list", "sink-inputs"], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return False
    text = result.stdout.lower()
    blocks = text.split("sink input #")
    return any(
        "discord" in block
        and ("media.role = \"communication\"" in block or "application.process.binary = \"discord\"" in block)
        for block in blocks
    )


def discord_calendar_title(config: dict) -> str | None:
    """Use the title of an active timed Calendar event for a Discord chat."""
    for event in now_events(config, require_meet=False):
        # Do not use all-day events as a meeting title for an arbitrary voice chat.
        if event.get("start", {}).get("dateTime") and event.get("end", {}).get("dateTime"):
            title = (event.get("summary") or "").strip()
            if title:
                return title
    return None


def choose_category(title: str, source: str) -> str | None:
    """Ask explicitly so every recording has an intentional vault classification."""
    command = [
        "zenity", "--list", "--radiolist", "--title=Start HushMeet recording?",
        "--text", f"{source} detected:\\n{title}\\n\\nChoose the Obsidian category:",
        "--column=Select", "--column=Category",
    ]
    for index, (_, label) in enumerate(CATEGORIES):
        command.extend(["TRUE" if index == 0 else "FALSE", label])
    response = subprocess.run(command, text=True, capture_output=True, check=False)
    if response.returncode != 0:
        detail = response.stderr.strip() or f"exit code {response.returncode}"
        print(f"hushmeet watcher: category prompt unavailable: {detail}", file=sys.stderr)
        return None
    selected = response.stdout.strip()
    return next((value for value, label in CATEGORIES if label == selected), None)


def record(category: str, title: str, source: str, meet_url: str | None = None) -> None:
    if active_recording():
        return
    start_recording(category, title, source, meet_url=meet_url)
    subprocess.run(
        ["notify-send", "HushMeet recording started", f"{title} ({source}). Use: hushmeet stop"],
        check=False,
    )


def clear_disconnect(source: str, meet_url: str | None = None) -> None:
    """Clear a pending end prompt only for the matching active recording."""
    data = active_recording()
    if not data or data.get("source") != source:
        return
    if meet_url and data.get("meet_url") != meet_url.rstrip("/"):
        return
    if "disconnect_started_at" in data or "disconnect_prompted" in data:
        data.pop("disconnect_started_at", None)
        data.pop("disconnect_prompted", None)
        write_active_recording(data)


def mark_disconnect(source: str, meet_url: str | None = None) -> None:
    """Start a continuous-disconnect timer only for the matching recording."""
    data = active_recording()
    if not data or data.get("source") != source:
        return
    if meet_url and data.get("meet_url") != meet_url.rstrip("/"):
        return
    if "disconnect_started_at" not in data:
        data["disconnect_started_at"] = datetime.now(timezone.utc).isoformat()
        write_active_recording(data)


def confirm_stop(title: str, source: str) -> bool:
    """Return True only when the user explicitly approves ending the recording."""
    response = subprocess.run(
        [
            "zenity", "--question", "--title=Stop HushMeet recording?",
            "--text", f"{source} appears to have ended.\n\nStop recording:\n{title}?",
            "--ok-label=Stop recording", "--cancel-label=Keep recording",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return response.returncode == 0


def prompt_after_disconnect_grace(grace_seconds: int) -> None:
    """Prompt once after a source has been absent continuously for the grace period."""
    data = active_recording()
    if not data or data.get("disconnect_prompted"):
        return
    started = data.get("disconnect_started_at")
    if not started:
        return
    try:
        disconnected_at = datetime.fromisoformat(started)
    except ValueError:
        data.pop("disconnect_started_at", None)
        write_active_recording(data)
        return
    if datetime.now(timezone.utc) - disconnected_at < timedelta(seconds=grace_seconds):
        return
    data["disconnect_prompted"] = True
    write_active_recording(data)
    if confirm_stop(data.get("title", "this meeting"), data.get("source", "The meeting")):
        stop_recording()
    else:
        subprocess.run(
            ["notify-send", "HushMeet continues recording", "Use: hushmeet stop when the meeting is finished."],
            check=False,
        )


def event_id(event: dict) -> str:
    return event.get("id") or event.get("htmlLink") or event.get("summary", "Google Meet")


def handle_meet_join(meet_url: str) -> int:
    """Prompt only after the browser extension confirms a joined Google Meet call."""
    config = load_config()
    normalized = meet_url.rstrip("/")
    clear_disconnect("Google Meet", normalized)
    event = next((event for event in now_events(config) if normalized in event_meet_urls(event)), None)
    if not event:
        print("No current Calendar event matched this Google Meet URL; no recording started.", file=sys.stderr)
        return 1
    title = event.get("summary") or "Google Meet"
    category = choose_category(title, "Joined Google Meet calendar event")
    if category:
        record(category, title, "Google Meet", normalized)
    return 0


def handle_meet_left(meet_url: str) -> int:
    """Start the grace timer after the browser confirms that a Meet call was left."""
    mark_disconnect("Google Meet", meet_url.rstrip("/"))
    return 0


def watch() -> int:
    config = load_config()
    poll_seconds = int(config.get("poll_seconds", 20))
    grace_seconds = int(config.get("disconnect_grace_seconds", DEFAULT_DISCONNECT_GRACE_SECONDS))
    discord_prompted = False
    while True:
        try:
            in_discord_voice = discord_in_voice()
            if in_discord_voice:
                clear_disconnect("Discord")
            if in_discord_voice and not discord_prompted:
                discord_prompted = True
                try:
                    title = discord_calendar_title(config) or "Discord voice chat"
                except Exception as exc:
                    print(f"hushmeet watcher: Calendar title unavailable for Discord: {exc}", file=sys.stderr)
                    title = "Discord voice chat"
                category = choose_category(title, "Discord communication audio")
                if category:
                    record(category, title, "Discord")
            elif not in_discord_voice:
                discord_prompted = False
                mark_disconnect("Discord")
            prompt_after_disconnect_grace(grace_seconds)
        except Exception as exc:
            print(f"hushmeet watcher: {exc}", file=sys.stderr)
        time.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true", help="stop the watcher-started recording")
    parser.add_argument("--meet-url", help="handle a confirmed joined Google Meet URL")
    parser.add_argument("--meet-left-url", help="handle a confirmed left Google Meet URL")
    args = parser.parse_args()
    if args.stop:
        if stop_recording():
            print("HushMeet stop signal sent; recording will be queued for nightly processing.")
        else:
            print("No watcher-started recording is active.")
        return 0
    if args.meet_url:
        return handle_meet_join(args.meet_url)
    if args.meet_left_url:
        return handle_meet_left(args.meet_left_url)
    return watch()


if __name__ == "__main__":
    raise SystemExit(main())
