#!/home/ephy/dev/hushnote/venv/bin/python3
"""Authorize HushMeet to read Calendar event titles through read-only OAuth."""

import json
from pathlib import Path

from hushmeet_calendar_policy import (
    CALENDAR_SCOPES,
    allowed_google_account,
    write_authorized_account_state,
)

CONFIG_PATH = Path.home() / ".config/hushmeet/watcher.json"
SCOPES = CALENDAR_SCOPES


def verified_google_account(credentials, allowed_account: str) -> str:
    """Verify the OpenID identity returned with the Calendar authorization."""
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token as google_id_token

    token = getattr(credentials, "id_token", None)
    client_id = getattr(credentials, "client_id", None)
    if not token or not client_id:
        raise SystemExit(
            "Google did not return a verifiable identity token. "
            "Run hushmeet-calendar-auth again."
        )
    try:
        claims = google_id_token.verify_oauth2_token(token, Request(), client_id)
    except Exception as exc:
        raise SystemExit(
            "Google did not return a verifiable identity token. "
            "Run hushmeet-calendar-auth again."
        ) from exc
    email = claims.get("email")
    if not claims.get("email_verified") or not isinstance(email, str):
        raise SystemExit("The selected Google account is not authorized for HushMeet Calendar access.")
    account = email.strip().casefold()
    if account != allowed_account:
        raise SystemExit("The selected Google account is not authorized for HushMeet Calendar access.")
    return account


def main() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CONFIG_PATH.exists():
        raise SystemExit(f"Missing {CONFIG_PATH}; copy watcher.example.json first.")
    config = json.loads(CONFIG_PATH.read_text())
    allowed_account = allowed_google_account(config)
    client_secret = Path(config["google_client_secret_file"]).expanduser()
    token_path = Path(config["google_token_file"]).expanduser()
    if not client_secret.exists():
        raise SystemExit(
            f"Missing {client_secret}. Create a Google OAuth Desktop client and save its downloaded JSON there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
    credentials = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )
    if not credentials.refresh_token:
        raise SystemExit(
            "Google did not return a refresh token. Revoke the existing HushMeet "
            "grant in your Google Account, then run this command again."
        )
    authorized_account = verified_google_account(credentials, allowed_account)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json())
    token_path.chmod(0o600)
    write_authorized_account_state(authorized_account, credentials.refresh_token)
    print(f"Calendar read-only authorization saved to {token_path}")


if __name__ == "__main__":
    main()
