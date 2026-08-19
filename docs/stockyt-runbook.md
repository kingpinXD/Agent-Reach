# Runbook: YouTube finance channels → stock ticker leaderboards

This is a step-by-step guide. Follow it top to bottom. Every command is exact — copy it, fill in the one or two values marked `<like_this>`, and run it. If a step's check fails, stop and read the Troubleshooting section at the bottom before continuing.

## What this does

It watches 7 fixed finance YouTube channels, pulls the transcripts of their recent videos, and turns those transcripts into two ranked lists: stocks the channels are **bullish** on and stocks they are **bearish** on. A stock ranks high when several different channels talk about it AND give real reasoning (a thesis, a catalyst, a price target) — not just when it gets name-dropped a lot.

There are three steps, each is one slash command:

1. `/yt-channels N` — get the last N weeks of videos as a list of links
2. `/yt-transcripts` — turn those links into transcripts
3. `/yt-tickers` — read the transcripts, pull out tickers, rank them

All output files land in one folder: `~/Downloads/YoutubeSummaries/`.

## Before you start: check the tools are there

Run these four checks. All four must pass. If any fails, see Troubleshooting.

```bash
agent-reach --version          # expect: Agent Reach v1.7.x
which yt-dlp                    # expect: a path (e.g. /opt/homebrew/bin/yt-dlp)
which ffmpeg                    # expect: a path (only needed for the rare no-captions video)
ls ~/.claude/commands/yt-channels.md ~/.claude/commands/yt-transcripts.md ~/.claude/commands/yt-tickers.md
```

You do NOT need any API key for the normal case. The ticker-reading step is done by Claude subagents inside the session, not an external service. An API key (Groq/OpenAI) is only needed for the rare case of a video that has no captions at all — see the whisper note in Troubleshooting.

The 7 channels are already built into the app (`agent_reach/stockyt/config.py`). You never pass them or look them up:
Tom Nash, ClearValue Tax, Meet Kevin, Financial Education, Joseph Carlson, Ticker Symbol: YOU, Stock Moe.

---

## Step 1 — Get the recent videos (`/yt-channels N`)

`N` is how many weeks back to look. Example for the last 3 weeks: `/yt-channels 3`.

The command runs this underneath:

```bash
agent-reach channels --weeks <N> \
  -o ~/Downloads/YoutubeSummaries/channels-links.txt \
  --channels-json ~/Downloads/YoutubeSummaries/channels-<N>w.json
```

**What it produces:**
- `~/Downloads/YoutubeSummaries/channels-links.txt` — one YouTube URL per line. This feeds Step 2.
- `~/Downloads/YoutubeSummaries/channels-<N>w.json` — the same videos with channel + date, for reference.

**Check before moving on:** the links file exists and has lines in it.
```bash
wc -l ~/Downloads/YoutubeSummaries/channels-links.txt
```
A number greater than 0 means Step 1 worked. If it's 0, no videos were found in the window — try a larger `N`.

**About the cache (why re-runs are fast):** the app keeps a per-channel cache under `~/Downloads/YoutubeSummaries/.cache/channels/`. When you re-run a week later, it only fetches the new week and reuses everything older. This is automatic — do nothing.

Only pass `--no-cache` if you want a clean re-pull from scratch (ignores the cache).

---

## Step 2 — Get the transcripts (`/yt-transcripts`)

Run `/yt-transcripts` with no argument — it defaults to the links file from Step 1.

This step splits the links across a team of subagents (default 30 at once) so it's fast, then each one runs:

```bash
agent-reach youtube --file <chunk_of_links.txt> -o <chunk_output.jsonl>
```

and the results are combined into one file.

**What it produces:**
- `~/Downloads/YoutubeSummaries/transcripts.jsonl` — one line per video. Each line has the URL, video id, channel name, the transcript text, and a `status` field (`ok` or `error`).

**Check before moving on:** the file exists and the number of `ok` lines is close to the number of links from Step 1.
```bash
wc -l ~/Downloads/YoutubeSummaries/transcripts.jsonl
grep -c '"status":"ok"' ~/Downloads/YoutubeSummaries/transcripts.jsonl
```
A few `error` lines are fine (a video may have no captions). If almost everything is an error, stop — something is wrong with yt-dlp (see Troubleshooting).

---

## Step 3 — Extract and rank the tickers (`/yt-tickers`)

Run `/yt-tickers` with no argument — it defaults to `transcripts.jsonl` from Step 2.

This is the only step with a "thinking" part. It works in three moves, and the command does them for you:

1. **Split** the transcripts into chunks (app does this, no thinking):
   ```bash
   agent-reach split-corpus --input <transcripts.jsonl> --out-dir <RUN_DIR> --chunks 30
   ```
   This writes `authors.json` (used to fairly compare channels of different sizes) and `chunk_01.jsonl … chunk_NN.jsonl` into a fresh `RUN_DIR` like `/tmp/ticker-run-<timestamp>/`.

2. **Extract** — spawn one subagent per chunk, all at once. Each subagent reads its chunk and, for every video, writes out which tickers were discussed and HOW (bullish or bearish, how strong, the reasoning/thesis, any catalyst or price target). Each writes to its own `extract_NN.jsonl` in `RUN_DIR`. If a subagent fails, retry it once, then move on. **This is the one step a model actually has to do — everything else is a plain command.**

3. **Grade** — the app does the ranking math, no thinking:
   ```bash
   agent-reach grade --extract-dir <RUN_DIR> --authors <RUN_DIR>/authors.json \
     -o ~/Downloads/YoutubeSummaries/tickers-<timestamp>.json
   ```

**What it produces:**
- `~/Downloads/YoutubeSummaries/tickers-<timestamp>.json` — the full ranked data.
- `~/Downloads/YoutubeSummaries/tickers-<timestamp>.md` — the same thing as two readable tables (bullish + bearish). This is the deliverable to show the user.

**Check — this is the important one.** The `grade` command prints an **Integrity** line. It must say `OK` (missing = 0). That means every video was accounted for. If it says `FAIL` or `missing > 0`, some chunks never produced results — re-run those subagents (move 2 above) before showing anyone the leaderboard. Do not present a clean-looking leaderboard over a FAIL.

---

## The whole thing, start to finish

For the last 3 weeks:

```
/yt-channels 3
/yt-transcripts
/yt-tickers
```

Then open `~/Downloads/YoutubeSummaries/tickers-<timestamp>.md` and show the two tables.

---

## How the ranking works (so you can explain a result)

You don't compute this by hand — `agent-reach grade` does. But when someone asks "why is GOOGL ranked #1", this is the answer:

- A stock scores on **breadth × strength**: how many different channels take a side on it, times how strong and well-reasoned each channel's take is.
- "Well-reasoned" is the dominant factor. A mention with a real thesis, a catalyst, and a price target counts far more than a bare name-drop. A pure name-drop counts almost nothing, no matter how many times it's repeated.
- Channels are size-normalized. Stock Moe posts ~140 videos to Joseph Carlson's ~2, so raw counts would let one loud channel dominate. The math uses "fraction of that channel's videos that mention the stock" instead.
- A stock can appear on BOTH lists if channels disagree — it's flagged as **contested**. Those are often the most interesting.
- Scores are scaled 0–100 within each list and tiered: S ≥ 80, A ≥ 60, B ≥ 40, C below 40.

The exact weights live at the top of `agent_reach/stockyt/grading.py` and are tunable. Re-grading with different weights is cheap — reuse the `extract_*.jsonl` files and just re-run `agent-reach grade`, skip the extraction.

---

## Troubleshooting

**`agent-reach: command not found`** — the app isn't installed. Install it editable so it tracks the repo:
`pipx install -e /Users/tanmay/IdeaProjects/kingpinXD/Agent-Reach`

**`agent-reach --version` shows an old number** — a stale Python cache can pin the old version. Clear it:
`find /Users/tanmay/IdeaProjects/kingpinXD/Agent-Reach -name __pycache__ -type d` then remove those dirs (in this environment `rm` may be blocked — move them aside with `mv <dir> /tmp/` instead).

**Almost every transcript is an `error`** — yt-dlp needs a JavaScript runtime for YouTube. Make sure `node` or `deno` is on PATH; `agent-reach install` sets up `~/.config/yt-dlp/config` with `--js-runtimes node`.

**A video has no captions at all** — the transcript step falls back to Whisper (audio transcription), which needs `ffmpeg` plus a Groq or OpenAI key (`agent-reach configure groq-key`). Without a key, that one video comes back empty and is skipped. The run still completes. This is rare for these channels — they almost always have auto-captions.

**Asked for a bigger window than last time and got fewer videos than expected** — this was a real bug (a small cached window wouldn't back-fill when you later asked for a bigger one). It's fixed as of v1.7.2. If you're on an older build, work around it with `--no-cache` on the bigger pull.

**Older videos all show the same date** — yt-dlp's `approximate_date` buckets anything older than ~2 weeks into "x weeks ago", so the 3rd week and beyond is fuzzy. Fine for week-level granularity; don't trust the exact day on old videos.

---

## Where things live

- App code: `/Users/tanmay/IdeaProjects/kingpinXD/Agent-Reach/agent_reach/stockyt/`
- Commands (symlinked into `~/.claude/commands/`): `Agent-Reach/commands/yt-{channels,transcripts,tickers}.md`
- All run output: `~/Downloads/YoutubeSummaries/`
- Cache: `~/Downloads/YoutubeSummaries/.cache/channels/`
- Branch: `feat/stockyt-channels-grading` on the personal fork `kingpinXD/Agent-Reach`
- Deeper history and design notes: `~/.claude/session-logs/agent-reach.md`
