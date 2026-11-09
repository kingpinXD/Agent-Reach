# Runbook: YouTube finance channels → stock ticker leaderboards

This is a step-by-step guide. Follow it top to bottom. Every command is exact — copy it, fill in the one or two values marked `<like_this>`, and run it. If a step's check fails, stop and read the Troubleshooting section at the bottom before continuing.

## What this does

It watches 7 fixed finance YouTube channels, pulls the transcripts of their recent videos, and turns those transcripts into two ranked lists: stocks the channels are **bullish** on and stocks they are **bearish** on. A stock ranks high when several different channels talk about it AND give real reasoning (a thesis, a catalyst, a price target) — not just when it gets name-dropped a lot.

There are three steps, each is one slash command:

1. `/yt-channels N` (or `/yt-channels YYYYMMDD`) — get the recent videos as a list of links
2. `/yt-transcripts` — turn those links into transcripts
3. `/yt-tickers` — read the transcripts, pull out tickers, have a second agent check them, rank them

All output files land in one folder: `"$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries"`. The folder name has a space, so **always keep that path in double quotes** in a shell command.

## Before you start: check the tools are there

Run these three checks. All three must pass. If any fails, see Troubleshooting.

```bash
agent-reach stockyt --help      # expect: a list with channels, transcripts, split-corpus, review-apply, grade
which ffmpeg                    # expect: a path (only needed for the rare no-captions video)
ls .agents/skills/yt-channels/SKILL.md .agents/skills/yt-transcripts/SKILL.md .agents/skills/yt-tickers/SKILL.md
```

Run the last one from the repo root. `.claude/skills` is a link to `.agents/skills`, so Claude Code finds the three commands there.

You do not need a separate `yt-dlp`. agent-reach installs a pinned yt-dlp as one of its own dependencies and runs that one.

You do NOT need any API key for the normal case. Reading tickers and checking them is done by Claude subagents inside the session, not an external service. An API key (Groq/OpenAI) is only needed for the rare case of a video that has no captions at all — see the Whisper note in Troubleshooting.

The 7 channels are already built into the app (`agent_reach/stockyt/config.py`). You never pass them or look them up:
Tom Nash, ClearValue Tax, Meet Kevin, Financial Education, Joseph Carlson, Ticker Symbol: YOU, Stock Moe.

---

## Step 1 — Get the recent videos (`/yt-channels`)

Pick the window one of two ways:
- **Weeks** — `/yt-channels 3` keeps the last 3 weeks.
- **A start day** — `/yt-channels 20261106` keeps videos uploaded on 6 Nov 2026 or later. Use this when the window starts at an event, like a market move.

The command runs this underneath (one of the two window flags):

```bash
agent-reach stockyt channels --weeks <N> \
  -o "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/channels-links.txt" \
  --channels-json "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/channels-<N>w.json"

agent-reach stockyt channels --since <YYYYMMDD> \
  -o "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/channels-links.txt" \
  --channels-json "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/channels-since-<YYYYMMDD>.json"
```

A bad or future `--since` day stops with an error before anything is fetched. Upload dates have no time of day, so the start day is kept whole.

**What it produces:**
- `channels-links.txt` — one YouTube URL per line. This feeds Step 2.
- `channels-<label>.json` — the same videos with channel + date, for reference.

**Check before moving on:** the links file exists and has lines in it.
```bash
wc -l "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/channels-links.txt"
```
A number greater than 0 means Step 1 worked. If it's 0, no videos were found in the window — try a larger window.

**About the cache (why re-runs are fast):** the app keeps a per-channel cache under `"$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/.cache/channels/"`. When you re-run a week later, it only fetches the new week and reuses everything older. This is automatic — do nothing.

Pass `--no-cache` in two cases: you want a clean re-pull from scratch, or **the cache's newest video is older than the start of your window**. In the second case a short pull would sit in the cache with a gap behind it, and a later longer run would stop at the cached videos and never fill the gap.

---

## Step 2 — Get the transcripts (`/yt-transcripts`)

Run `/yt-transcripts` with no argument — it defaults to the links file from Step 1.

This step splits the links across a team of subagents (default 30 at once) so it's fast, then each one runs:

```bash
agent-reach stockyt transcripts --file <chunk_of_links.txt> -o <chunk_output.jsonl>
```

and the results are combined into one file.

**What it produces:**
- `transcripts-<timestamp>.jsonl` in the output folder — one line per video. Each line has the URL, video id, channel name, the transcript text, `source` (`subtitles` or `whisper`) and a `status` field (`ok` or `error`).

**Check before moving on:** the file exists and the number of `ok` lines is close to the number of links from Step 1.
```bash
wc -l "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/transcripts-<timestamp>.jsonl"
grep -c '"status": "ok"' "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/transcripts-<timestamp>.jsonl"
```
A few `error` lines are fine (a video may have no captions). If almost everything is an error, stop — something is wrong with yt-dlp (see Troubleshooting).

---

## Step 3 — Extract, review and rank the tickers (`/yt-tickers`)

Run `/yt-tickers` with no argument — it defaults to the newest transcripts file from Step 2.

It works in five moves, and the command does them for you:

1. **Split** the transcripts into chunks (app, no thinking):
   ```bash
   agent-reach stockyt split-corpus --input "<transcripts.jsonl>" --out-dir <RUN_DIR> --chunks 30
   ```
   This writes `authors.json` (used to fairly compare channels of different sizes) and `chunk_01.jsonl … chunk_NN.jsonl` into a fresh `RUN_DIR` like `/tmp/ticker-run-<timestamp>/`.

2. **Extract** — one subagent per chunk, all at once (named `extract-01`, `extract-02`, …). Each reads its chunk and, for every video, writes which tickers were discussed and HOW (bullish or bearish, how strong, the reasoning, any catalyst or price target) to `extract_NN.jsonl`. A failed subagent is retried once.

3. **Review, two rounds** — a second subagent per chunk (`review-01`, …) checks the extraction against the transcript: are these the right tickers, and is the sentiment what the author actually said. Every finding needs a word-for-word quote from the transcript.
   - Round 1: the reviewer lists findings; the extractor accepts or rejects each one, with a quote when it rejects.
   - Round 2: the reviewer holds or withdraws each rejected finding, and may check rows its accepted findings changed; the extractor answers those.
   - Always exactly two rounds. Nobody edits the extract file; each agent writes only its own small JSON file.

4. **Apply the review** — the app, not an agent, decides what happens to each row:
   ```bash
   agent-reach stockyt review-apply --run-dir <RUN_DIR>
   ```
   Accepted findings correct, drop or add a row. A finding the two agents still disagree on after round 2 makes the row **disputed**: it is left out of the scores and only counted. It must print `Review: OK`. On `FAIL` it lists what is missing (an unanswered finding, a missing file); fix that chunk and re-run it.

5. **Grade** — the app does the ranking math on the reviewed rows:
   ```bash
   agent-reach stockyt grade --reviewed --extract-dir <RUN_DIR> --authors <RUN_DIR>/authors.json \
     -o "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/tickers-<timestamp>.json"
   ```

**What it produces:**
- `tickers-<timestamp>.json` — the full ranked data, the review counts, and the disputed count per ticker.
- `tickers-<timestamp>.md` — the same thing as two readable tables (bullish + bearish), with a review line and a Disputed column. This is the deliverable to show the user.

**Check — this is the important one.** The `grade` command prints an **Integrity** line. It must say `OK` (missing = 0). That means every video was accounted for. If it says `FAIL` or `missing > 0`, some chunks never produced results — re-run those subagents (move 2 above) before showing anyone the leaderboard. `grade --reviewed` also refuses to run unless `review-apply` said OK. Do not present a clean-looking leaderboard over a FAIL.

---

## The whole thing, start to finish

For the last 3 weeks:

```
/yt-channels 3
/yt-transcripts
/yt-tickers
```

Then open `tickers-<timestamp>.md` in the output folder and show the two tables.

---

## How the ranking works (so you can explain a result)

You don't compute this by hand — `agent-reach stockyt grade` does. But when someone asks "why is GOOGL ranked #1", this is the answer:

- A stock scores on **breadth × strength**: how many different channels take a side on it, times how strong and well-reasoned each channel's take is.
- "Well-reasoned" is the dominant factor. A mention with a real thesis, a catalyst, and a price target counts far more than a bare name-drop. A pure name-drop counts almost nothing, no matter how many times it's repeated.
- Channels are size-normalized. Stock Moe posts ~140 videos to Joseph Carlson's ~2, so raw counts would let one loud channel dominate. The math uses "share of that channel's videos that take this side on the stock" instead. A bearish video counts only for the bearish list, a neutral mention for neither. One extra video is added to every channel's count, so a channel with one video in the window can't score full coverage on everything it mentions.
- A stock can appear on BOTH lists when it has bullish and bearish videos. It's flagged as **contested** when channels disagree overall. Those are often the most interesting.
- Each list shows its own side's strongest thesis, and its Breadth reads "2 bull · 3/5": two channels bullish, three of five mention it.
- Rows the review dropped or left disputed count for nothing. Disputed ones show up only in the Disputed column.
- Scores are scaled 0–100 within each list and tiered: S ≥ 80, A ≥ 60, B ≥ 40, C below 40.

The exact weights live at the top of `agent_reach/stockyt/grading.py` and are tunable. Re-grading with different weights is cheap — reuse the `reviewed_*.jsonl` files and just re-run `agent-reach stockyt grade --reviewed`, skipping extraction and review.

---

## Troubleshooting

**`agent-reach stockyt` says unknown command** — the `agent-reach` on PATH is an old build without this pipeline. Use a build of the fork's stockyt branch: either `pipx install -e <clone>` on that branch, or a venv in the clone (`python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`) and run `.venv/bin/agent-reach`.

**Every video has date `NA`, or `channels` says "no usable rows"** — an old yt-dlp is running. yt-dlp 2026.06.09 (Homebrew) returns `NA` for every upload date in the channel listing. The app runs the yt-dlp pinned in agent-reach's own dependencies (2026.8.19, which returns real dates), so this only happens on an old build that still calls the `yt-dlp` on PATH. Fix: use a current build (see above). Check the pinned one with `<python of that install> -m yt_dlp --version`.

**Almost every transcript is an `error`** — yt-dlp needs a JavaScript runtime for YouTube. Make sure `deno` or `node` is on PATH; the app passes `--js-runtimes node` itself when only node is there.

**A video has no captions at all** — the transcript step can fall back to Whisper (audio transcription), which needs `ffmpeg` plus a Groq or OpenAI key (`agent-reach configure groq-key`). Whisper is off by default in `/yt-transcripts`. Without it, that one video comes back as an `error` row and is skipped. The run still completes. This is rare for these channels — they almost always have auto-captions.

**Asked for a bigger window than last time and got fewer videos than expected** — this was a real bug (a small cached window wouldn't back-fill when you later asked for a bigger one). It's fixed as of v1.7.2. If the cache has a gap (see the cache note in Step 1), use `--no-cache`.

**Older videos all show the same date** — yt-dlp's `approximate_date` buckets anything older than ~2 weeks into "x weeks ago", so the 3rd week and beyond is fuzzy. Fine for week-level granularity; don't trust the exact day on old videos.

**`review-apply` says `Review: FAIL`** — read the listed problems. Usually an agent died before writing its file, or a rejected finding got no hold/withdraw answer in round 2. Re-run that chunk's round and run `review-apply` again.

---

## Keeping the fork in sync with upstream

The fork tracks `Panniantong/Agent-Reach` (git remote `upstream`). Upstream's own `agent-reach channels` and `agent-reach youtube` are different commands (list platforms; read/search/subtitle a video). That is why this pipeline lives under `agent-reach stockyt`: an upstream merge never collides with it. To sync: `git fetch upstream && git merge upstream/main`, then run the suite. Upstream owns `agent_reach/channels/youtube.py` and the main CLI; our code is in `agent_reach/stockyt/` and the one `stockyt` line in `agent_reach/runtime/cli.py`.

---

## Where things live

- App code: `agent_reach/stockyt/` in the personal fork `kingpinXD/Agent-Reach`
- Commands: `.agents/skills/yt-{channels,transcripts,tickers}/SKILL.md` (`.claude/skills` links there)
- All run output: `"$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries"`
- Cache: `"$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/.cache/channels/"`
- Deeper history and design notes: `~/.agents/repos/agent-reach.md`
