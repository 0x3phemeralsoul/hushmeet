#!/home/ephy/dev/hushnote/venv/bin/python3
"""Authorize HushMeet to read Calendar event titles through read-only OAuth."""

import json
from pathlib import Path

CONFIG_PATH = Path.home() / ".config/hushmeet/watcher.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.events.readonly"]


def main() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CONFIG_PATH.exists():
        raise SystemExit(f"Missing {CONFIG_PATH}; copy watcher.example.json first.")
    config = json.loads(CONFIG_PATH.read_text())
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
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json())
    token_path.chmod(0o600)
    print(f"Calendar read-only authorization saved to {token_path}")


if __name__ == "__main__":
    main()
