#!/usr/bin/env python3
"""
Morning News Podcast builder.

Reads newsletter summaries from episodes/incoming/, speaks them with Kokoro
using two alternating voices, writes an MP3 into docs/audio/, and regenerates
the podcast RSS feed at docs/feed.xml.

Input file naming:   YYYY-MM-DD-<newsletter-key>.md      e.g. 2026-09-16-1440.md
Optional frontmatter lines (DATE:, STATUS:) and a --- separator are stripped.
The first paragraph of the body is used as the episode title line.

Run:  python podcast.py            (processes everything in episodes/incoming/)
      python podcast.py --dry-run  (no speech; writes a silent placeholder)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
INCOMING = ROOT / "episodes" / "incoming"
ARCHIVE = ROOT / "episodes" / "archive"
DOCS = ROOT / "docs"
AUDIO_DIR = DOCS / "audio"
MANIFEST = ROOT / "episodes.json"

FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)$")


# --------------------------------------------------------------------------
# config and manifest
# --------------------------------------------------------------------------

def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_manifest() -> list[dict]:
    if not MANIFEST.exists():
        return []
    with MANIFEST.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("episodes", []) if isinstance(data, dict) else data


def save_manifest(episodes: list[dict]) -> None:
    episodes.sort(key=lambda e: (e["published"], e["id"]), reverse=True)
    with MANIFEST.open("w", encoding="utf-8") as fh:
        json.dump({"episodes": episodes}, fh, indent=2)
        fh.write("\n")


# --------------------------------------------------------------------------
# text handling
# --------------------------------------------------------------------------

def strip_frontmatter(raw: str) -> str:
    """Remove leading KEY: VALUE lines and an optional --- separator."""
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line == "---":
            i += 1
            break
        if re.match(r"^[A-Z][A-Z _-]*:", line):
            i += 1
            continue
        break
    return "\n".join(lines[i:]).strip()


def apply_pronunciation(text: str, rules: list[list[str]]) -> str:
    """Apply replacements. Alphanumeric terms match on word boundaries."""
    for find, replace in rules:
        if re.fullmatch(r"[\w.-]+", find):
            text = re.sub(rf"\b{re.escape(find)}\b", replace, text)
        else:
            text = text.replace(find, replace)
    return re.sub(r"[ \t]{2,}", " ", text)


def split_turns(body: str) -> list[str]:
    """One turn per paragraph, blank-line separated."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    return [re.sub(r"\s+", " ", p) for p in parts]


def make_title(first_turn: str, newsletter_label: str, date_str: str) -> str:
    """Build a readable episode title from the summary's opening line."""
    sentences = re.split(r"(?<=\.)\s+", first_turn)
    subject = ""
    for s in sentences:
        low = s.lower()
        if "newsletter" in low or newsletter_label.lower() in low:
            continue
        if re.match(r"^[A-Z][a-z]+ \w+, twenty", s):
            continue
        subject = s.strip().rstrip(".")
        break
    parsed = datetime.strptime(date_str, "%Y-%m-%d")
    pretty_date = f"{parsed.strftime('%b')} {parsed.day}"
    return f"{newsletter_label} — {pretty_date}" + (f": {subject}" if subject else "")


# --------------------------------------------------------------------------
# speech
# --------------------------------------------------------------------------

class Speaker:
    """Lazily built Kokoro pipelines, one per language code."""

    def __init__(self, voices: dict, sample_rate: int):
        self.voices = voices
        self.sample_rate = sample_rate
        self._pipelines: dict[str, object] = {}

    def _pipeline(self, lang: str):
        if lang not in self._pipelines:
            from kokoro import KPipeline
            self._pipelines[lang] = KPipeline(lang_code=lang)
        return self._pipelines[lang]

    def say(self, text: str, voice_key: str, out_path: Path) -> None:
        import numpy as np
        import soundfile as sf

        voice = self.voices[voice_key]
        pipeline = self._pipeline(voice["lang"])
        chunks = []
        for _graphemes, _phonemes, audio in pipeline(text, voice=voice["id"]):
            chunks.append(np.asarray(audio, dtype="float32"))
        if not chunks:
            raise RuntimeError(f"Kokoro produced no audio for: {text[:60]!r}")
        sf.write(str(out_path), np.concatenate(chunks), self.sample_rate)


def silent_wav(path: Path, seconds: float, sample_rate: int) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"anullsrc=r={sample_rate}:cl=mono", "-t", f"{seconds}", str(path)],
        check=True,
    )


def placeholder_wav(path: Path, seconds: float, sample_rate: int) -> None:
    """Dry-run stand-in for speech. A near-inaudible tone, not digital silence:
    loudnorm cannot normalize an all-silent track and aborts the encoder."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency=220:sample_rate={sample_rate}",
         "-t", f"{seconds}", "-af", "volume=-30dB", str(path)],
        check=True,
    )


def concat_to_mp3(wavs: list[Path], out_mp3: Path, cfg: dict) -> None:
    audio_cfg = cfg["audio"]
    listing = out_mp3.parent / f".{out_mp3.stem}.concat.txt"
    listing.write_text(
        "".join(f"file '{w.as_posix()}'\n" for w in wavs), encoding="utf-8"
    )
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(listing),
             "-af", f"loudnorm=I={audio_cfg['loudness_target_lufs']}:TP=-1.5:LRA=11",
             "-ar", str(audio_cfg["sample_rate"]), "-ac", "1",
             "-b:a", audio_cfg["mp3_bitrate"], str(out_mp3)],
            check=True,
        )
    except Exception:
        # never leave a truncated or corrupt file where the feed can link to it
        out_mp3.unlink(missing_ok=True)
        raise
    finally:
        listing.unlink(missing_ok=True)
    if not out_mp3.exists() or out_mp3.stat().st_size < 1024:
        out_mp3.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg produced no usable audio")


def duration_seconds(path: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return int(round(float(out.stdout.strip())))


def hhmmss(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


# --------------------------------------------------------------------------
# feed
# --------------------------------------------------------------------------

def build_feed(cfg: dict, episodes: list[dict]) -> str:
    show = cfg["show"]
    base = show["site_url"].rstrip("/") + "/"
    now = format_datetime(datetime.now(timezone.utc))

    items = []
    for ep in episodes:
        published = format_datetime(
            datetime.fromisoformat(ep["published"]).astimezone(timezone.utc)
        )
        items.append(f"""    <item>
      <title>{escape(ep['title'])}</title>
      <description>{escape(ep['description'])}</description>
      <pubDate>{published}</pubDate>
      <enclosure url="{escape(base + ep['audio'])}" length="{ep['bytes']}" type="audio/mpeg"/>
      <guid isPermaLink="false">{escape(ep['id'])}</guid>
      <itunes:author>{escape(show['author'])}</itunes:author>
      <itunes:duration>{hhmmss(ep['duration'])}</itunes:duration>
      <itunes:explicit>{show['explicit']}</itunes:explicit>
    </item>""")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(show['title'])}</title>
    <link>{escape(base)}</link>
    <description>{escape(show['description'])}</description>
    <language>{show['language']}</language>
    <lastBuildDate>{now}</lastBuildDate>
    <atom:link href="{escape(base + show['feed_path'])}" rel="self" type="application/rss+xml"/>
    <itunes:author>{escape(show['author'])}</itunes:author>
    <itunes:summary>{escape(show['description'])}</itunes:summary>
    <itunes:owner>
      <itunes:name>{escape(show['author'])}</itunes:name>
      <itunes:email>{escape(show['email'])}</itunes:email>
    </itunes:owner>
    <itunes:image href="{escape(base + show['cover_file'])}"/>
    <itunes:category text="{escape(show['category'])}"/>
    <itunes:explicit>{show['explicit']}</itunes:explicit>
{chr(10).join(items)}
  </channel>
</rss>
"""


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def process_file(src: Path, cfg: dict, speaker: Speaker | None) -> dict:
    match = FILENAME_RE.match(src.stem)
    if not match:
        raise ValueError(
            f"{src.name}: expected a name like 2026-09-16-1440.md"
        )
    date_str, key = match.groups()
    label = cfg["newsletters"].get(key, key.replace("-", " ").title())

    body = strip_frontmatter(src.read_text(encoding="utf-8"))
    if not body:
        raise ValueError(f"{src.name}: no content after frontmatter")

    turns = split_turns(body)
    spoken = [apply_pronunciation(t, cfg["pronunciation"]) for t in turns]

    episode_id = f"{date_str}-{key}"
    out_mp3 = AUDIO_DIR / f"{episode_id}.mp3"
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    sample_rate = cfg["audio"]["sample_rate"]
    pause = cfg["audio"]["pause_between_turns_ms"] / 1000.0

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        pieces: list[Path] = []
        gap = tmpdir / "gap.wav"
        silent_wav(gap, pause, sample_rate)

        for idx, text in enumerate(spoken):
            voice_key = "primary" if idx % 2 == 0 else "secondary"
            piece = tmpdir / f"turn{idx:03d}.wav"
            if speaker is None:
                placeholder_wav(piece, 1.0, sample_rate)
            else:
                speaker.say(text, voice_key, piece)
            if pieces:
                pieces.append(gap)
            pieces.append(piece)

        concat_to_mp3(pieces, out_mp3, cfg)

    return {
        "id": episode_id,
        "title": make_title(turns[0], label, date_str),
        "description": turns[0],
        "newsletter": key,
        "published": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "audio": f"audio/{out_mp3.name}",
        "bytes": out_mp3.stat().st_size,
        "duration": duration_seconds(out_mp3),
        "turns": len(turns),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="skip Kokoro, emit silent placeholder audio")
    args = parser.parse_args()

    cfg = load_config()
    sources = sorted(
        p for p in INCOMING.glob("*") if p.suffix.lower() in {".md", ".txt"}
    )
    if not sources:
        print("Nothing in episodes/incoming/, nothing to do.")
        return 0

    speaker = None if args.dry_run else Speaker(cfg["voices"], cfg["audio"]["sample_rate"])
    episodes = load_manifest()
    known = {e["id"] for e in episodes}
    made = 0

    for src in sources:
        try:
            entry = process_file(src, cfg, speaker)
        except Exception as exc:  # keep going; one bad file must not stop the rest
            print(f"FAILED {src.name}: {exc}", file=sys.stderr)
            continue

        episodes = [e for e in episodes if e["id"] != entry["id"]]
        episodes.append(entry)
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(ARCHIVE / src.name))
        made += 1
        verb = "replaced" if entry["id"] in known else "added"
        print(f"{verb}: {entry['id']}  {hhmmss(entry['duration'])}  {entry['turns']} turns")

    if not made:
        print("No episodes were produced.", file=sys.stderr)
        return 1

    save_manifest(episodes)
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / cfg["show"]["feed_path"]).write_text(
        build_feed(cfg, episodes), encoding="utf-8"
    )
    print(f"feed rebuilt with {len(episodes)} episodes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
