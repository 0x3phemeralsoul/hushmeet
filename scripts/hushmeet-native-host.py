#!/usr/bin/env python3
"""Chrome native-messaging bridge for Google Meet call join and leave signals."""

import json
import struct
import subprocess
import sys
from pathlib import Path

WATCHER = Path("/home/ephy/dev/hushnote/scripts/hushmeet-watcher.py")
PYTHON = Path("/home/ephy/dev/hushnote/venv/bin/python3")


def read_message() -> dict | None:
    header = sys.stdin.buffer.read(4)
    if len(header) != 4:
        return None
    length = struct.unpack("@I", header)[0]
    payload = sys.stdin.buffer.read(length)
    return json.loads(payload.decode("utf-8"))


def write_message(payload: dict) -> None:
    encoded = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("@I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def main() -> None:
    while message := read_message():
        meet_url = message.get("meet_url", "")
        if not meet_url.startswith("https://meet.google.com/"):
            write_message({"ok": False, "error": "Invalid Google Meet URL"})
            continue
        action = message.get("action", "joined")
        if action not in {"joined", "left"}:
            write_message({"ok": False, "error": "Invalid Google Meet action"})
            continue
        argument = "--meet-url" if action == "joined" else "--meet-left-url"
        result = subprocess.run(
            [str(PYTHON), str(WATCHER), argument, meet_url],
            text=True,
            capture_output=True,
            check=False,
        )
        write_message({"ok": result.returncode == 0, "message": result.stderr.strip()})


if __name__ == "__main__":
    main()
