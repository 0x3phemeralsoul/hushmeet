#!/usr/bin/env python3
"""Prompt to record current Google Calendar Meet events or Discord voice activity."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from hushmeet_calendar import calendar_events, load_config
from hushmeet_calendar_policy import calendar_reauthorization_due
from hushmeet_recording import (
    CATEGORIES,
    active_recording,
    start_recording,
    stop_recording,
    write_active_recording,
)


DEFAULT_DISCONNECT_GRACE_SECONDS = 90
DEFAULT_DISCORD_CONFIRM_SECONDS = 30
DEFAULT_CALENDAR_HEALTH_CHECK_SECONDS = 6 * 60 * 60
DEFAULT_CALENDAR_ERROR_REMINDER_SECONDS = 60 * 60
DEFAULT_CALENDAR_REAUTH_REMINDER_SECONDS = 24 * 60 * 60


def now_events(config: dict, require_meet: bool = True) -> list[dict]:
    """Return Calendar events currently in progress for watcher decisions."""
    return calendar_events(config, require_meet=require_meet)


def calendar_error_notice(exc: Exception) -> str:
    """Return a safe, actionable notification for Calendar failures."""
    detail = str(exc).lower()
    auth_markers = (
        "invalid_grant",
        "google calendar token is invalid",
        "google calendar is not authorized",
        "google calendar authorization is not bound to the allowed account",
        "missing allowed_google_account",
    )
    if any(marker in detail for marker in auth_markers):
        return "Google Calendar access needs reauthorization. Run: hushmeet-calendar-auth"
    return "Google Calendar could not be checked. Open the HushMeet watcher logs for details."


def calendar_reauthorization_notice(config: dict) -> str | None:
    if calendar_reauthorization_due(config):
        return "HushMeet Calendar test authorization is due for renewal. Run: hushmeet-calendar-auth"
    return None


def send_notification(title: str, body: str) -> None:
    subprocess.run(["notify-send", title, body], check=False)


def check_calendar_health(
    config: dict,
    last_error: str | None,
    last_notification_at: float,
    now: float,
    reminder_seconds: int,
    reauthorization_reminder_seconds: int = DEFAULT_CALENDAR_REAUTH_REMINDER_SECONDS,
) -> tuple[str | None, float]:
    """Validate read-only Calendar access without starting a recording."""
    try:
        now_events(config)
        reauthorization_notice = calendar_reauthorization_notice(config)
    except Exception as exc:
        notice = calendar_error_notice(exc)
        print(f"hushmeet watcher: Calendar health check failed: {exc}", file=sys.stderr)
        if notice != last_error or now - last_notification_at >= reminder_seconds:
            send_notification("HushMeet Calendar needs attention", notice)
            return notice, now
        return notice, last_notification_at
    if reauthorization_notice:
        print(f"hushmeet watcher: {reauthorization_notice}", file=sys.stderr)
        if (
            reauthorization_notice != last_error
            or now - last_notification_at >= reauthorization_reminder_seconds
        ):
            send_notification("HushMeet Calendar needs attention", reauthorization_notice)
            return reauthorization_notice, now
        return reauthorization_notice, last_notification_at
    return None, 0.0


def event_meet_urls(event: dict) -> set[str]:
    urls = {event.get("hangoutLink", "")}
    for point in event.get("conferenceData", {}).get("entryPoints", []):
        urls.add(point.get("uri", ""))
    return {url.rstrip("/") for url in urls if url}


DISCORD_APPLICATION_PROPERTY = re.compile(
    r'^\s*(?:application\.name|application\.process\.binary|application\.icon_name)\s*=\s*"discord"\s*$',
    re.IGNORECASE | re.MULTILINE,
)
COMMUNICATION_ROLE_PROPERTY = re.compile(
    r'^\s*media\.role\s*=\s*"communication"\s*$',
    re.IGNORECASE | re.MULTILINE,
)


def pactl_stream_blocks(stream_type: str, header: str) -> list[str]:
    result = subprocess.run(["pactl", "list", stream_type], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return []
    return result.stdout.split(header)[1:]


def is_discord_communication_stream(block: str) -> bool:
    return bool(
        DISCORD_APPLICATION_PROPERTY.search(block)
        and COMMUNICATION_ROLE_PROPERTY.search(block)
    )


def discord_in_voice() -> bool:
    """Require both Discord communication playback and microphone capture."""
    playback_streams = pactl_stream_blocks("sink-inputs", "Sink Input #")
    capture_streams = pactl_stream_blocks("source-outputs", "Source Output #")
    return any(is_discord_communication_stream(block) for block in playback_streams) and any(
        is_discord_communication_stream(block) for block in capture_streams
    )


def discord_calendar_title(config: dict) -> str | None:
    for event in now_events(config, require_meet=False):
        if event.get("start", {}).get("dateTime") and event.get("end", {}).get("dateTime"):
            title = (event.get("summary") or "").strip()
            if title:
                return title
    return None


def choose_category(title: str, source: str) -> str | None:
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
    data = active_recording()
    if not data or data.get("source") != source:
        return
    if meet_url and data.get("meet_url") != meet_url.rstrip("/"):
        return
    if "disconnect_started_at" not in data:
        data["disconnect_started_at"] = datetime.now(timezone.utc).isoformat()
        write_active_recording(data)


def confirm_stop(title: str, source: str) -> bool:
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


def handle_meet_join(meet_url: str) -> int:
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
    mark_disconnect("Google Meet", meet_url.rstrip("/"))
    return 0


def watch() -> int:
    config = load_config()
    poll_seconds = int(config.get("poll_seconds", 20))
    grace_seconds = int(config.get("disconnect_grace_seconds", DEFAULT_DISCONNECT_GRACE_SECONDS))
    discord_confirm_seconds = int(config.get("discord_confirm_seconds", DEFAULT_DISCORD_CONFIRM_SECONDS))
    calendar_health_check_seconds = int(
        config.get("calendar_health_check_seconds", DEFAULT_CALENDAR_HEALTH_CHECK_SECONDS)
    )
    calendar_error_reminder_seconds = int(
        config.get("calendar_error_reminder_seconds", DEFAULT_CALENDAR_ERROR_REMINDER_SECONDS)
    )
    calendar_reauthorization_reminder_seconds = int(
        config.get(
            "calendar_reauthorization_reminder_seconds",
            DEFAULT_CALENDAR_REAUTH_REMINDER_SECONDS,
        )
    )
    discord_prompted = False
    discord_voice_started_at: float | None = None
    next_calendar_health_check = 0.0
    last_calendar_error: str | None = None
    last_calendar_notification_at = 0.0
    while True:
        try:
            current_time = time.monotonic()
            if current_time >= next_calendar_health_check:
                last_calendar_error, last_calendar_notification_at = check_calendar_health(
                    config,
                    last_calendar_error,
                    last_calendar_notification_at,
                    current_time,
                    calendar_error_reminder_seconds,
                    calendar_reauthorization_reminder_seconds,
                )
                retry_after = calendar_error_reminder_seconds if last_calendar_error else calendar_health_check_seconds
                next_calendar_health_check = current_time + retry_after
            in_discord_voice = discord_in_voice()
            if in_discord_voice:
                clear_disconnect("Discord")
                if discord_voice_started_at is None:
                    discord_voice_started_at = current_time
            else:
                discord_prompted = False
                discord_voice_started_at = None
                mark_disconnect("Discord")
            if (
                in_discord_voice
                and not discord_prompted
                and discord_voice_started_at is not None
                and current_time - discord_voice_started_at >= discord_confirm_seconds
            ):
                discord_prompted = True
                try:
                    title = discord_calendar_title(config) or "Discord voice chat"
                except Exception as exc:
                    print(f"hushmeet watcher: Calendar title unavailable for Discord: {exc}", file=sys.stderr)
                    title = "Discord voice chat"
                category = choose_category(title, "Discord communication audio")
                if category:
                    record(category, title, "Discord")
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
