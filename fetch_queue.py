#!/usr/bin/env python3
"""
Pull new summaries from the Google Doc queue into episodes/incoming/.

The queue doc is plain text, link-shared, and split into blocks by a header line:

    === EPISODE 2026-09-16 1440 ===

Everything between one header and the next is that episode's spoken summary.
Anything before the first header is ignored, so the doc can carry instructions
at the top.

Blocks whose episode id already appears in episodes.json are skipped, so old
blocks are harmless and the doc never has to be cleaned up on a schedule.

Standard library only. This runs before pip install in the workflow.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
INCOMING = ROOT / "episodes" / "incoming"
ARCHIVE = ROOT / "episodes" / "archive"
MANIFEST = ROOT / "episodes.json"

HEADER_RE = re.compile(
    r"^===\s*EPISODE\s+(\d{4}-\d{2}-\d{2})\s+([A-Za-z0-9._-]+)\s*===\s*$"
)
TIMEOUT = 60


def export_url(doc_id: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id}/export?format=txt"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "morning-news-podcast"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    return raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")


def parse_blocks(text: str) -> list[tuple[str, str, str]]:
    """Return (date, key, body) for each block, in document order."""
    blocks: list[tuple[str, str, str]] = []
    current: tuple[str, str] | None = None
    buf: list[str] = []

    for line in text.split("\n"):
        match = HEADER_RE.match(line.strip())
        if match:
            if current:
                blocks.append((current[0], current[1], "\n".join(buf).strip()))
            current = (match.group(1), match.group(2))
            buf = []
        elif current:
            buf.append(line)

    if current:
        blocks.append((current[0], current[1], "\n".join(buf).strip()))
    return blocks


def already_known(episode_id: str) -> bool:
    """True if this episode was built before, or is already waiting to be built."""
    if (INCOMING / f"{episode_id}.md").exists():
        return True
    if (ARCHIVE / f"{episode_id}.md").exists():
        return True
    if MANIFEST.exists():
        try:
            with MANIFEST.open(encoding="utf-8") as fh:
                data = json.load(fh)
            episodes = data.get("episodes", []) if isinstance(data, dict) else data
            if any(e.get("id") == episode_id for e in episodes):
                return True
        except (json.JSONDecodeError, OSError) as exc:
            print(f"warning: could not read episodes.json ({exc})", file=sys.stderr)
    return False


def main() -> int:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        cfg = json.load(fh)

    doc_id = cfg.get("queue", {}).get("google_doc_id", "").strip()
    if not doc_id or doc_id.startswith("PASTE"):
        print("No queue document configured yet. Nothing to fetch.")
        return 0

    valid_keys = set(cfg.get("newsletters", {}))

    try:
        text = fetch(export_url(doc_id))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        # A fetch failure must not fail the run. Any episodes already waiting
        # in episodes/incoming/ should still get built.
        print(f"warning: could not read the queue document ({exc})", file=sys.stderr)
        return 0

    if "<html" in text[:400].lower():
        print(
            "warning: the queue document returned a web page rather than text. "
            "It is probably not shared as 'Anyone with the link'.",
            file=sys.stderr,
        )
        return 0

    blocks = parse_blocks(text)
    if not blocks:
        print("Queue document has no episode blocks.")
        return 0

    INCOMING.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped = 0

    for date_str, key, body in blocks:
        episode_id = f"{date_str}-{key}"
        if key not in valid_keys:
            print(f"skipping {episode_id}: '{key}' is not a known newsletter key",
                  file=sys.stderr)
            continue
        if not body:
            print(f"skipping {episode_id}: empty block", file=sys.stderr)
            continue
        if already_known(episode_id):
            skipped += 1
            continue

        (INCOMING / f"{episode_id}.md").write_text(
            f"DATE: {date_str}\nSOURCE: queue\n---\n{body}\n", encoding="utf-8"
        )
        print(f"queued: {episode_id}")
        written += 1

    print(f"{written} new, {skipped} already built")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
