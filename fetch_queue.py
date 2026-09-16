#!/usr/bin/env python3
"""
Pull new summaries from Google Drive into episodes/incoming/.

v1.2 design: the newsletter chat cannot edit a Google Doc, so it creates ONE NEW
FILE PER EPISODE inside a link-shared Drive folder. The file title is

    EPISODE 2026-09-16 1440

and the file body is the spoken summary, blank lines between paragraphs kept.

The folder must be shared "Anyone with the link, Viewer". Files created inside
it inherit that, so nothing per file needs sharing. No API key, no secret.

Episodes already built, or already waiting in episodes/incoming/, are skipped,
so old files are harmless and the folder never needs cleaning.

The old single queue doc (queue.google_doc_id) is still read if configured,
using the "=== EPISODE yyyy-mm-dd key ===" block format. Either source works.

Standard library only. This runs before pip install in the workflow.
"""

from __future__ import annotations

import html
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
TIMEOUT = 60

HEADER_RE = re.compile(
    r"^===\s*EPISODE\s+(\d{4}-\d{2}-\d{2})\s+([A-Za-z0-9._-]+)\s*===\s*$"
)
TITLE_RE = re.compile(
    r"^\s*EPISODE\s+(\d{4}-\d{2}-\d{2})\s+([A-Za-z0-9_-]+)(?:\.(?:txt|md))?\s*$",
    re.IGNORECASE,
)
# A Drive file id: letters, digits, - and _, long.
ID = r"[A-Za-z0-9_-]{20,}"


# ---------------------------------------------------------------- fetching

def fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (morning-news-podcast)",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    return raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")


def looks_like_html(text: str) -> bool:
    head = text[:500].lower()
    return "<html" in head or "<!doctype html" in head


# ---------------------------------------------------------------- folder listing

def parse_folder_listing(page: str) -> list[dict]:
    """
    Parse Drive's embeddedfolderview page into [{id, title, kind}].

    Each entry carries an id="entry-<fileId>", a link to the file, and a
    flip-entry-title div. We split on the entry marker so a change in the
    order of attributes inside one entry does not break parsing.
    """
    entries: list[dict] = []
    seen: set[str] = set()
    chunks = re.split(r'id="entry-', page)[1:]
    for chunk in chunks:
        m_id = re.match(rf"({ID})\"", chunk)
        if not m_id:
            continue
        file_id = m_id.group(1)
        m_title = re.search(r'class="flip-entry-title"[^>]*>(.*?)</div>', chunk, re.S)
        title = html.unescape(re.sub(r"<[^>]+>", "", m_title.group(1))).strip() if m_title else ""
        m_href = re.search(r'href="([^"]+)"', chunk)
        href = html.unescape(m_href.group(1)) if m_href else ""
        if "/document/d/" in href:
            kind = "gdoc"
        elif "/drive/folders/" in href:
            kind = "folder"
        else:
            kind = "file"
        if file_id not in seen:
            seen.add(file_id)
            entries.append({"id": file_id, "title": title, "kind": kind})
    return entries


def list_folder(folder_id: str) -> list[dict] | None:
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}"
    try:
        page = fetch(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"warning: could not open the episodes folder ({exc})", file=sys.stderr)
        return None
    entries = parse_folder_listing(page)
    if not entries:
        lowered = page.lower()
        if "access denied" in lowered or "accounts.google.com" in lowered or "sign in" in lowered:
            print(
                "warning: Google refused to list the episodes folder. It is probably "
                "not shared as 'Anyone with the link'.",
                file=sys.stderr,
            )
        elif "flip-entry" not in lowered:
            print(
                "warning: the folder page did not look like a Drive folder listing. "
                f"First 300 characters: {page[:300]!r}",
                file=sys.stderr,
            )
        else:
            print("Episodes folder is empty.")
    return entries


def download_entry(entry: dict) -> str | None:
    if entry["kind"] == "gdoc":
        url = f"https://docs.google.com/document/d/{entry['id']}/export?format=txt"
    else:
        url = f"https://drive.google.com/uc?export=download&id={entry['id']}"
    try:
        text = fetch(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"warning: could not download '{entry['title']}' ({exc})", file=sys.stderr)
        return None
    if looks_like_html(text):
        print(
            f"warning: '{entry['title']}' returned a web page instead of text. "
            "Check the folder sharing.",
            file=sys.stderr,
        )
        return None
    return text


def clean_body(text: str) -> str:
    """Strip an optional header line or frontmatter the writer may have left in."""
    lines = text.strip("\n").split("\n")
    if lines and HEADER_RE.match(lines[0].strip()):
        lines = lines[1:]
    body = "\n".join(lines).strip()
    m = re.match(r"^(?:(?:DATE|STATUS|SOURCE):[^\n]*\n)+---\n", body)
    if m:
        body = body[m.end():]
    # Collapse runs of 3+ newlines to one blank line, keep paragraph breaks.
    body = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", body)
    return body.strip()


# ---------------------------------------------------------------- old doc queue

def parse_blocks(text: str) -> list[tuple[str, str, str]]:
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


# ---------------------------------------------------------------- bookkeeping

def load_manifest_ids() -> set[str]:
    if not MANIFEST.exists():
        return set()
    try:
        with MANIFEST.open(encoding="utf-8") as fh:
            data = json.load(fh)
        episodes = data.get("episodes", []) if isinstance(data, dict) else data
        return {e.get("id") for e in episodes if isinstance(e, dict)}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"warning: could not read episodes.json ({exc})", file=sys.stderr)
        return set()


def already_known(episode_id: str, manifest_ids: set[str]) -> bool:
    return (
        episode_id in manifest_ids
        or (INCOMING / f"{episode_id}.md").exists()
        or (ARCHIVE / f"{episode_id}.md").exists()
    )


def queue_episode(date_str: str, key: str, body: str, source: str) -> None:
    INCOMING.mkdir(parents=True, exist_ok=True)
    episode_id = f"{date_str}-{key}"
    (INCOMING / f"{episode_id}.md").write_text(
        f"DATE: {date_str}\nSOURCE: {source}\n---\n{body}\n", encoding="utf-8"
    )
    print(f"queued: {episode_id} (from {source})")


# ---------------------------------------------------------------- main

def main() -> int:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        cfg = json.load(fh)

    queue = cfg.get("queue", {})
    folder_id = str(queue.get("google_folder_id", "")).strip()
    doc_id = str(queue.get("google_doc_id", "")).strip()
    valid_keys = set(cfg.get("newsletters", {}))
    manifest_ids = load_manifest_ids()
    written = skipped = 0

    # 1. The episodes folder, one file per episode.
    if folder_id and not folder_id.startswith("PASTE"):
        entries = list_folder(folder_id) or []
        print(f"Episodes folder lists {len(entries)} file(s).")
        for entry in entries:
            m = TITLE_RE.match(entry["title"])
            if not m:
                print(f"ignoring '{entry['title']}': title is not 'EPISODE yyyy-mm-dd key'")
                continue
            date_str, key = m.group(1), m.group(2).lower()
            episode_id = f"{date_str}-{key}"
            if already_known(episode_id, manifest_ids):
                skipped += 1
                continue
            text = download_entry(entry)
            if text is None:
                continue
            body = clean_body(text)
            if key not in valid_keys:
                print(f"ignoring {episode_id}: '{key}' is not a newsletter key "
                      f"(downloaded fine, {len(body)} characters)")
                continue
            if not body:
                print(f"skipping {episode_id}: file is empty", file=sys.stderr)
                continue
            queue_episode(date_str, key, body, "drive-folder")
            written += 1

    # 2. The old single queue doc, if still configured.
    if doc_id and not doc_id.startswith("PASTE"):
        try:
            text = fetch(f"https://docs.google.com/document/d/{doc_id}/export?format=txt")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            print(f"warning: could not read the queue document ({exc})", file=sys.stderr)
            text = ""
        if text and not looks_like_html(text):
            for date_str, key, body in parse_blocks(text):
                episode_id = f"{date_str}-{key}"
                if key not in valid_keys or not body:
                    continue
                if already_known(episode_id, manifest_ids):
                    skipped += 1
                    continue
                queue_episode(date_str, key, body, "queue-doc")
                written += 1

    print(f"{written} new, {skipped} already built or waiting")
    # Never fail the run: anything already in episodes/incoming/ should still build.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
