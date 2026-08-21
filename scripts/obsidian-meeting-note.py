#!/usr/bin/env python3
"""Build one Obsidian meeting note after HushNote finishes processing."""

import json
import re
import sys
from pathlib import Path


def yaml_scalar(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def note_filename(title: str) -> str:
    """Return a readable, cross-platform-safe Obsidian filename."""
    cleaned = re.sub(r'[\\/:*?"<>|]', "-", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return f"{cleaned[:120] or 'Meeting'}.md"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: obsidian-meeting-note.py SUMMARY.md")

    summary_path = Path(sys.argv[1]).resolve()
    if not summary_path.is_file() or not summary_path.name.endswith("_summary.md"):
        raise SystemExit(f"Invalid summary path: {summary_path}")

    meeting_dir = summary_path.parent
    stem = summary_path.name.removesuffix("_summary.md")
    metadata_path = meeting_dir / f"{stem}_metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    title = metadata.get("title") or stem.replace("meeting_", "Meeting ")
    category = meeting_dir.parents[1].name
    date = metadata.get("date", "")

    transcript_candidates = [
        meeting_dir / f"{stem}.txt",
        meeting_dir / f"{stem}.md",
    ]
    transcript_path = next((path for path in transcript_candidates if path.exists()), None)
    transcript = transcript_path.read_text().strip() if transcript_path else "Transcript unavailable."
    audio_candidates = [
        meeting_dir / f"{stem}_trimmed.mp3",
        meeting_dir / f"{stem}.mp3",
        meeting_dir / f"{stem}.wav",
    ]
    audio_path = next((path for path in audio_candidates if path.exists()), None)

    links = []
    if audio_path:
        links.append(f"![[{audio_path.name}]]")
    if transcript_path:
        links.append(f"[[{transcript_path.name}|Plain-text transcript]]")
    links.append(f"[[{summary_path.name}|Generated summary]]")

    note = "\n".join(
        [
            "---",
            "type: meeting",
            f"category: {yaml_scalar(category)}",
            f"date: {yaml_scalar(date)}",
            f"title: {yaml_scalar(title)}",
            "tags:",
            "  - meeting",
            f"  - {category.replace(' ', '-').lower()}",
            "status: processed",
            "---",
            "",
            f"# {title}",
            "",
            "## Files",
            *links,
            "",
            summary_path.read_text().strip(),
            "",
            "## Transcript",
            transcript,
            "",
        ]
    )
    note_path = meeting_dir / note_filename(title)
    if transcript_path and note_path == transcript_path:
        note_path = meeting_dir / note_filename(f"{title} note")
    note_path.write_text(note)
    print(note_path)


if __name__ == "__main__":
    main()
