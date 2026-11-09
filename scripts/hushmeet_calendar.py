"""Read-only Calendar helpers shared by HushMeet desktop clients."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CONFIG_PATH = Path.home() / ".config/hushmeet/watcher.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.events.readonly"]


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Missing {CONFIG_PATH}. Copy watcher.example.json there and set google_client_secret_file."
        )
    return json.loads(CONFIG_PATH.read_text())


def calendar_events(
    config: dict[str, Any],
    *,
    require_meet: bool = True,
    before_minutes: int = 1,
    after_minutes: int = 1,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return calendar events in the requested UTC window."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Missing Google dependencies; run the HushMeet Python installation step.") from exc

    token_path = Path(config["google_token_file"]).expanduser()
    if not token_path.exists():
        raise RuntimeError("Google Calendar is not authorized. Run hushmeet-calendar-auth first.")
    credentials = Credentials.from_authorized_user_file(token_path, SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        token_path.write_text(credentials.to_json())
        token_path.chmod(0o600)
    if not credentials.valid:
        raise RuntimeError("Google Calendar token is invalid. Run hushmeet-calendar-auth again.")

    current_time = now or datetime.now(timezone.utc)
    service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    response = service.events().list(
        calendarId=config.get("calendar_id", "primary"),
        timeMin=(current_time - timedelta(minutes=before_minutes)).isoformat(),
        timeMax=(current_time + timedelta(minutes=after_minutes)).isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=20,
    ).execute()
    events = []
    for event in response.get("items", []):
        conference = event.get("conferenceData", {})
        entry_points = conference.get("entryPoints", [])
        has_meet = "meet.google.com" in event.get("hangoutLink", "") or any(
            "meet.google.com" in point.get("uri", "") for point in entry_points
        )
        if not require_meet or has_meet:
            events.append(event)
    return events


def _event_datetime(event: dict[str, Any], field: str) -> datetime | None:
    value = event.get(field, {}).get("dateTime")
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def nearby_events(config: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    """Return events within five minutes, favoring one currently in progress."""
    current_time = now or datetime.now(timezone.utc)
    events = calendar_events(
        config,
        require_meet=False,
        before_minutes=5,
        after_minutes=5,
        now=current_time,
    )

    def sort_key(event: dict[str, Any]) -> tuple[int, float]:
        start = _event_datetime(event, "start")
        end = _event_datetime(event, "end")
        if start and end and start <= current_time <= end:
            return (0, 0.0)
        if start:
            return (1, abs((start - current_time).total_seconds()))
        return (2, float("inf"))

    return sorted(events, key=sort_key)
