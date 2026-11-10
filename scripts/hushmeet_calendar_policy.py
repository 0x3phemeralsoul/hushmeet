"""Fail-closed Google Calendar account binding for HushMeet."""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


CALENDAR_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar.events.readonly",
]
AUTHORIZED_ACCOUNT_STATE_PATH = (
    Path.home() / ".local/state/hushmeet/calendar-authorized-account.json"
)
ACCOUNT_BINDING_ERROR = (
    "Google Calendar authorization is not bound to the allowed account. "
    "Run hushmeet-calendar-auth again."
)


def allowed_google_account(config: dict) -> str:
    """Return the one Google account permitted to supply Calendar events."""
    configured = config.get("allowed_google_account")
    if not isinstance(configured, str):
        raise RuntimeError(
            "Missing allowed_google_account in ~/.config/hushmeet/watcher.json."
        )
    account = configured.strip().casefold()
    if not account or "@" not in account:
        raise RuntimeError(
            "allowed_google_account in ~/.config/hushmeet/watcher.json must be an email address."
        )
    return account


def refresh_token_fingerprint(refresh_token: str) -> str:
    if not refresh_token:
        raise RuntimeError(ACCOUNT_BINDING_ERROR)
    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()


def read_authorized_account_state() -> dict:
    try:
        payload = json.loads(AUTHORIZED_ACCOUNT_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(ACCOUNT_BINDING_ERROR) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(ACCOUNT_BINDING_ERROR)
    return payload


def write_authorized_account_state(
    email: str, refresh_token: str, authorized_at: datetime | None = None
) -> None:
    """Persist the verified account and a non-secret binding to its refresh token."""
    account = email.strip().casefold()
    if not account or "@" not in account:
        raise RuntimeError(ACCOUNT_BINDING_ERROR)
    authorized_at = authorized_at or datetime.now(timezone.utc)
    if authorized_at.tzinfo is None:
        raise ValueError("authorized_at must include a timezone")
    payload = {
        "email": account,
        "refresh_token_sha256": refresh_token_fingerprint(refresh_token),
        "authorized_at": authorized_at.astimezone(timezone.utc).isoformat(),
    }
    AUTHORIZED_ACCOUNT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = AUTHORIZED_ACCOUNT_STATE_PATH.with_name(
        f".{AUTHORIZED_ACCOUNT_STATE_PATH.name}.tmp"
    )
    temporary_path.write_text(json.dumps(payload, sort_keys=True) + "\n")
    temporary_path.chmod(0o600)
    temporary_path.replace(AUTHORIZED_ACCOUNT_STATE_PATH)
    AUTHORIZED_ACCOUNT_STATE_PATH.chmod(0o600)


def verify_authorized_account_state(config: dict, refresh_token: str) -> str:
    """Reject tokens not authenticated for the configured Google account."""
    account = allowed_google_account(config)
    payload = read_authorized_account_state()

    state_account = payload.get("email")
    state_fingerprint = payload.get("refresh_token_sha256")
    expected_fingerprint = refresh_token_fingerprint(refresh_token)
    if (
        not isinstance(state_account, str)
        or not isinstance(state_fingerprint, str)
        or not hmac.compare_digest(state_account.casefold(), account)
        or not hmac.compare_digest(state_fingerprint, expected_fingerprint)
    ):
        raise RuntimeError(ACCOUNT_BINDING_ERROR)
    return account


def calendar_reauthorization_due(config: dict, now: datetime | None = None) -> bool:
    """Return whether an External/Testing token should be renewed before expiry.

    The setting is intentionally opt-in, because production refresh tokens do
    not have the seven-day test-user lifetime.
    """
    renewal_days = config.get("calendar_reauthorize_after_days")
    if renewal_days is None:
        return False
    try:
        renewal_interval = timedelta(days=float(renewal_days))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("calendar_reauthorize_after_days must be a positive number.") from exc
    if renewal_interval <= timedelta():
        raise RuntimeError("calendar_reauthorize_after_days must be a positive number.")

    authorized_at = read_authorized_account_state().get("authorized_at")
    if not isinstance(authorized_at, str):
        return True
    try:
        authorized_at = datetime.fromisoformat(authorized_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if authorized_at.tzinfo is None:
        return True
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    return now.astimezone(timezone.utc) >= authorized_at.astimezone(timezone.utc) + renewal_interval
