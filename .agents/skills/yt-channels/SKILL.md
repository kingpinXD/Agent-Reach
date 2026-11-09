---
name: yt-channels
description: Fetch all videos posted in the last N WEEKS, or since a given day, from the fixed set of finance YouTube channels, into a links file (cached, incremental). Auto-triggers when user says 'yt channels', 'fetch channel videos', 'download stock yt', 'last N weeks of videos', 'videos since <date>', 'recent videos from the channels', or similar. Arguments: A number = how many WEEKS back from today to fetch (e.g. '4'), OR a day as YYYYMMDD = keep videos uploaded on or after it (e.g. '20261106'). Optional: '-o <path>' links file, '--no-cache'. If omitted, ask for the window.
allowed-tools: Bash, Read, Write, Grep, Glob
---

# YouTube Channel Videos (last N weeks, or since a day): $ARGUMENTS

Fetch every video posted in the window — the last **N weeks**, or **since a day** — from the fixed channel set, newest-first, and write a flat links file that feeds `/yt-transcripts`. The concrete logic lives in the app (`agent_reach/stockyt/channels.py` + `cache.py`, tested) — this command just runs it, sanity-checks, and reports.

The 7 channels are a constant in the app (`agent_reach/stockyt/config.py`) — do NOT re-derive or pass them.

## Step 1: Resolve the window

`$ARGUMENTS` is either a number of weeks (integer → `--weeks <N>`, label `<N>w`) or a day `YYYYMMDD` (→ `--since <YYYYMMDD>`, label `since-<YYYYMMDD>`). A day is kept: `--since 20261106` keeps videos uploaded on 6 Nov and later. Pull optional `-o` / `--no-cache` out. If the window is missing or unclear, ask — don't guess.

## Step 2: Run the fetcher

```bash
agent-reach stockyt channels <--weeks N | --since YYYYMMDD> \
  -o "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/channels-links.txt" \
  --channels-json "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/channels-<label>.json"
```

The output folder has a space in its name — keep every path quoted exactly as shown.

What it does (deterministic, no LLM): for each channel it lists videos with the yt-dlp that ships with agent-reach (`--flat-playlist --extractor-args "youtubetab:approximate_date"`: newest-first, dates included, no per-video fetch), **early-stops** once it hits a video already in the cache OR older than the cutoff, merges new videos into the per-channel cache ("$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/.cache/channels/"), and writes the in-window URLs. A malformed or future `--since` day is an error before anything is fetched.

**Cache is the point:** a re-run a week later only fetches the new week — everything older is reused. Don't pass `--no-cache` unless the user wants a clean re-pull, with one exception: **if the cache's newest video is older than the start of your window, pass `--no-cache`.** Otherwise the short pull lands in the cache with a gap behind it, and a later longer run stops at the cached videos and never fills that gap.

## Step 3: Report + sanity-check

The command prints a per-channel summary, a total, and an integrity line. Surface that to the user, and verify:
- **Boundary sanity** — each channel's oldest kept video sits near the window's start (proves pagination wasn't truncated; matters for high-frequency channels like Meet Kevin / Stock Moe).
- **Integrity** — total URLs == sum of per-channel counts.
- **Failed channels** — if the summary reports any channel that fell back to cache (fetch failed), call it out so the user knows that channel may be stale.

## Step 4: Hand off

The links file (`channels-links.txt`) is the input to `/yt-transcripts`. Offer to run the rest of the pipeline (`/yt-transcripts` → `/yt-tickers`) on it, or stop here if the user just wanted the list.

## Notes
- Output + cache live in "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries". Dates are exact for recent videos and approximate beyond ~2 weeks (from YouTube's relative "x weeks ago"). Upload dates have no time of day, so a `--since` day cannot split a day in two.
- Filters by upload date only — it does NOT judge whether a video is about stock picks. That's what `/yt-tickers` is for, downstream.
- Pairs with `/yt-transcripts` and `/yt-tickers`. Engine: `agent-reach stockyt` from the personal fork `kingpinXD/Agent-Reach`.
