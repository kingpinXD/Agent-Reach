---
description: "Fetch all videos posted in the last N WEEKS from the fixed set of finance YouTube channels, into a links file (cached, incremental). Auto-triggers when user says 'yt channels', 'fetch channel videos', 'download stock yt', 'last N weeks of videos', 'recent videos from the channels', or similar, with a number of weeks."
allowed-tools: Bash, Read, Write, Grep, Glob
argument-description: "A number = how many WEEKS back from today to fetch (e.g. '4'). Optional: '-o <path>' links file, '--no-cache'. If omitted, ask how many weeks."
---

# YouTube Channel Videos (last N weeks): $ARGUMENTS

Fetch every video posted in the last **N weeks** from the fixed channel set, newest-first, and write a flat links file that feeds `/yt-transcripts`. The concrete logic lives in the app (`agent_reach/stockyt/channels.py` + `cache.py`, tested) — this command just runs it, sanity-checks, and reports.

The 7 channels are a constant in the app (`agent_reach/stockyt/config.py`) — do NOT re-derive or pass them.

## Step 1: Resolve N

`$ARGUMENTS` is the number of weeks (integer). Pull optional `-o` / `--no-cache` out. If N is missing or unclear, ask — don't guess.

## Step 2: Run the fetcher

```bash
agent-reach channels --weeks <N> \
  -o ~/Downloads/YoutubeSummaries/channels-links.txt \
  --channels-json ~/Downloads/YoutubeSummaries/channels-<N>w.json
```

What it does (deterministic, no LLM): for each channel it lists videos via `yt-dlp --flat-playlist --extractor-args "youtubetab:approximate_date"` (newest-first, dates included, no per-video fetch), **early-stops** once it hits a video already in the cache OR older than the N-week cutoff, merges new videos into the per-channel cache (`~/Downloads/YoutubeSummaries/.cache/channels/`), and writes the in-window URLs.

**Cache is the point:** a re-run a week later only fetches the new week — everything older is reused. Don't pass `--no-cache` unless the user wants a clean re-pull.

## Step 3: Report + sanity-check

The command prints a per-channel summary, a total, and an integrity line. Surface that to the user, and verify:
- **Boundary sanity** — each channel's oldest kept video sits near the N-week boundary (proves pagination wasn't truncated; matters for high-frequency channels like Meet Kevin / Stock Moe).
- **Integrity** — total URLs == sum of per-channel counts.
- **Failed channels** — if the summary reports any channel that fell back to cache (fetch failed), call it out so the user knows that channel may be stale.

## Step 4: Hand off

The links file (`channels-links.txt`) is the input to `/yt-transcripts`. Offer to run the rest of the pipeline (`/yt-transcripts` → `/yt-tickers`) on it, or stop here if the user just wanted the list.

## Notes
- Output + cache live in `~/Downloads/YoutubeSummaries/`. Dates are approximate (from YouTube's relative "x weeks ago"), fine for week-granularity cutoffs.
- Filters by upload date only — it does NOT judge whether a video is about stock picks. That's what `/yt-tickers` is for, downstream.
- Pairs with `/yt-transcripts` and `/yt-tickers`. Engine: `/Users/tanmay/IdeaProjects/kingpinXD/Agent-Reach`.
