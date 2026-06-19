---
description: "Batch-fetch YouTube transcripts from a file of links (or pasted URLs) into JSONL, in parallel via a team of subagents, then compile + verify + summarize. Auto-triggers when user says 'yt transcripts', 'youtube transcripts', 'fetch transcripts', 'get transcripts for these videos', 'transcripts from this file', or similar, followed by a links file or YouTube URLs."
allowed-tools: Bash, Read, Write, Grep, Glob, Agent, Task
argument-description: "Path to a file of YouTube links (one per line), OR one/more pasted YouTube URLs. Optional: '-o <path>' output JSONL, '--whisper' to enable the Whisper fallback (default off), '--lang <code>' subtitle language, '--concurrency <n>' agent count (default 30). If omitted, ask for the links."
---

# YouTube Transcripts (parallel): $ARGUMENTS

You are the **orchestrator**. Split the link list across a team of subagents that each fetch a slice in parallel, then compile their outputs into one JSONL, verify nothing was lost, and report. The heavy lifting per link is done by the `agent-reach youtube` engine (local fork at `/Users/tanmay/IdeaProjects/kingpinXD/Agent-Reach`, installed editable as `agent-reach`).

Each link's record (transcript-only, keyed by url+video_id):
```json
{"url":"…","video_id":"…","channel":"…","channel_id":"…","transcript":"…","source":"subtitles|auto_subtitles|whisper","status":"ok"}
{"url":"…","video_id":"…","channel":null,"channel_id":null,"transcript":null,"source":null,"status":"error","error":"no transcript available"}
```

## Config
```
CONCURRENCY:   30          # target number of parallel subagents (override with --concurrency)
WHISPER:       off         # default subtitles/auto-subs only; --whisper enables fallback (needs ffmpeg + Groq key)
LANG:          en          # --lang to change
OUTPUT DIR:    ~/Downloads/YoutubeSummaries/        # final compiled JSONL
RUN DIR:       /tmp/yt-run-<timestamp>/             # canonical link list + per-agent batch files (intermediate)
```

## Step 1: Resolve input → canonical list

Parse `$ARGUMENTS`:
- A **file path** → read it. One URL per line; skip blank lines and `#` comments.
- **Pasted URLs** → collect them.
- **Empty/unclear** → ask the user. Don't guess.

Pull optional flags out of `$ARGUMENTS`: `-o`, `--whisper`, `--lang`, `--concurrency`.

Create the run dir `/tmp/yt-run-<timestamp>/` and write the de-duplicated, order-preserved list to `all-links.txt`. Let `N` = number of links. Tell the user `N` and the chosen concurrency.

## Step 2: Split into chunks

- Let `A = min(CONCURRENCY, N)` (no empty agents).
- Chunk size = `ceil(N / A)`. Write chunk files `batch_01.txt … batch_<A>.txt` in the run dir (contiguous slices, order preserved). Each agent owns exactly one chunk file and one output file `batch_NN.jsonl`.

Use a small shell/python step to do the split deterministically — do not eyeball it.

## Step 3: Fan out the team (single wave, concurrent)

Spawn **all `A` subagents in ONE message** (multiple Agent tool calls in one turn) so they run concurrently — this is the whole point; do not run them in sequential phases. Use `subagent_type: general-purpose`.

Each subagent prompt must contain its `batch_NN.txt` path, its `batch_NN.jsonl` output path, and these instructions:

> You fetch YouTube transcripts for one batch. Run EXACTLY:
> `cd /Users/tanmay/IdeaProjects/kingpinXD/Agent-Reach && agent-reach youtube --file <batch_NN.txt> -o <batch_NN.jsonl> --lang <LANG> [--no-whisper unless WHISPER on]`
> Then read your own `<batch_NN.jsonl>`. Return ONLY a compact JSON object (no prose):
> `{"batch":NN,"attempted":<count of input links>,"ok":<count status=ok>,"skipped":[{"video_id":…,"url":…,"reason":<error string>} …],"output_file":"<path>","line_count":<lines written>}`
> Do not summarize transcripts, do not do anything else. If the command errors out entirely, still return the JSON with whatever you have and an `"agent_error":"<msg>"` field.

## Step 4: Collect + retry dead agents (once)

Gather every subagent's JSON. A batch is **dead** if its agent returned null/crashed, reported `agent_error`, or its `batch_NN.jsonl` is missing or has fewer lines than `attempted`. For each dead batch: **re-spawn it once** (same prompt). If it still fails, record its chunk's links as **unrecovered** and move on — never block the whole run on one batch.

## Step 5: Compile

Concatenate `batch_01.jsonl … batch_<A>.jsonl` (in order) into the final output: the user's `-o` path, else `~/Downloads/YoutubeSummaries/transcripts-<timestamp>.jsonl` (mkdir -p first). Each batch file already holds both ok and error rows.

## Step 6: Verify (count integrity — this is mandatory)

Read the final JSONL and reconcile against `all-links.txt` by `video_id` (fall back to url). Compute:
- `present_ok`   = records with status ok
- `present_err`  = records with status error (these are the **skipped** videos, with reasons — aggregate from the batch reports + the error rows)
- `unrecovered`  = links from dead batches that produced no record
- `missing`      = any link in `all-links.txt` with NO record in the final file AND not in `unrecovered` (should be empty — if not, it's an integrity bug)

**Assert:** `len(present_ok) + len(present_err) + len(unrecovered) + len(missing) == N`, and that the set of all original video_ids equals `present ∪ skipped ∪ unrecovered ∪ missing`. If the assertion fails or `missing` is non-empty, say so loudly and list the offending links — do not present a clean summary over a lossy run.

## Step 7: Summary to user

Report concisely:
- `N` total → **X ok**, **Y skipped**, **Z unrecovered/missing**
- the skipped list with reasons (e.g. "no transcript available"), and unrecovered links if any
- distinct channels (top few by count)
- final output path
- one line confirming count integrity ("all N links accounted for") or the integrity failure

Then offer next steps (per-video summaries, combined digest, per-channel split) — but only if the user hasn't already said what they want.

## Notes
- Output (JSONL + any summaries) goes in `~/Downloads/YoutubeSummaries/`. Per-agent batch files and raw VTT/audio stay in `/tmp/` (the run dir), never in a project workspace.
- JSONL is append-as-you-go inside each batch, so a crash mid-batch keeps that batch's completed records — re-running only needs the unrecovered links.
- `--whisper` makes no-subtitle videos fall back to transcription (slower, needs ffmpeg + `agent-reach configure groq-key gsk_…`). Default off means no-caption videos come back as `skipped` error rows.
- This is a personal-fork command; engine behavior tracks `/Users/tanmay/IdeaProjects/kingpinXD/Agent-Reach`.
