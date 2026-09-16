# Morning News Podcast

Turns Jared's daily newsletter summaries into a real podcast feed. Drop a text
file in, get an MP3 and an updated RSS feed out. Runs entirely on GitHub Actions
free minutes, costs nothing.

**Feed address:** `https://jaredbond007.github.io/morning-news-podcast/feed.xml`

---

## How it works

1. A summary file lands in `episodes/incoming/`.
2. Pushing that file starts the **Publish episodes** workflow.
3. The workflow installs Kokoro and FFmpeg, runs `podcast.py`, and produces an MP3.
4. It commits the MP3 into `docs/audio/`, rebuilds `docs/feed.xml`, and pushes.
5. GitHub Pages serves `docs/`, so the episode appears in the feed.
6. The workflow then re-fetches the live feed and confirms the episode is really there
   before it calls the run a success.

One episode per newsletter. They publish as each newsletter arrives rather than
waiting for the slowest one.

## File naming

```
episodes/incoming/YYYY-MM-DD-<newsletter-key>.md
```

for example `2026-09-16-1440.md`. The keys are defined in `config.json`:
`1440`, `rundown-ai`, `superhuman`, `robotics`.

Leading `KEY: value` lines and a `---` separator are stripped. The first
paragraph becomes the episode title and description, so keep the existing
"date, newsletter name, subject" opening line.

Each blank-line-separated paragraph becomes one speaking turn, alternating
between the two voices.

## Voices

Set in `config.json`:

| role | voice | accent |
|---|---|---|
| primary | `af_heart` | American female |
| secondary | `bm_george` | British male |

Kokoro ships other voices. Change the `id` and matching `lang` (`a` American,
`b` British) to swap them.

## Pronunciation

`config.json` has a `pronunciation` list of find/replace pairs applied before
speech. Alphanumeric terms match on word boundaries, so `AI` does not touch
`AIR`. `1440` is spelled out as "fourteen forty" because Kokoro otherwise reads
it as four digits. Add a pair whenever something is read wrong.

## Running it by hand

```bash
pip install -r requirements.txt
python podcast.py              # build everything in episodes/incoming/
python podcast.py --dry-run    # skip speech, emit placeholder audio, test the plumbing
```

`--dry-run` needs no Kokoro install and is the fast way to check that parsing,
titles, the manifest and the feed are all correct.

## Layout

```
podcast.py                    the builder
config.json                   show metadata, voices, pronunciation
requirements.txt              Python packages
episodes/incoming/            drop summaries here
episodes/archive/             processed summaries move here
episodes.json                 episode manifest, source of truth for the feed
docs/                         published by GitHub Pages
  index.html                  landing page with the feed address
  cover.png                   show artwork
  feed.xml                    the RSS feed
  audio/                      the MP3s
.github/workflows/publish.yml the automation
```

## Notes

- A failed episode does not stop the others. It is reported and the run continues.
- A partially written MP3 is deleted rather than left where the feed could link to it.
- Reprocessing the same date and newsletter replaces that episode instead of duplicating it.
- Pushes made by the workflow do not retrigger the workflow, so there is no loop.
- Episodes are never deleted. Old summaries stay in `episodes/archive/`.
