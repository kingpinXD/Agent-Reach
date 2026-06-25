---
description: "Extract stock/crypto tickers from a corpus of YouTube transcripts (JSONL) in parallel via a team of subagents, then grade them into ranked Bullish/Bearish leaderboards using cross-author consensus. Auto-triggers when user says 'ticker extract', 'extract tickers', 'grade tickers', 'which stocks are they talking about', 'ticker leaderboard', or similar, pointed at a transcripts file."
allowed-tools: Bash, Read, Write, Grep, Glob, Agent, Task
argument-description: "Path to a transcripts JSONL (default ~/Downloads/YoutubeSummaries/transcripts.jsonl). Optional: '--concurrency <n>' (default 30), '-o <dir>' output dir, '--top <n>' leaderboard size."
---

# Ticker Extract & Grade: $ARGUMENTS

You are the **orchestrator**. From a corpus of YouTube transcripts, extract every investable ticker discussed, then grade them into **two leaderboards (Bullish / Bearish)** ranked primarily by *cross-author consensus* **weighted by the quality of the conversational context** around each mention. The principle: **extraction is LLM work (done by subagents), grading is deterministic math (done by you in a Python step)** — so the ranking is reproducible, not vibes.

**Context is the key signal.** A bare name-drop is nearly worthless; a reasoned thesis with a catalyst and a price target is gold. Two authors who actually *argue* a position must be able to outrank four authors who merely mention the ticker in passing. So extraction must capture the *reasoning* (thesis, catalyst, recommendation), and grading multiplies each mention's weight by how substantive its context is — raw mention counts never drive the rank on their own.

Tickers appear in transcripts as **spoken company names**, not `$SYMBOL` (verified: zero `$TICKER` hits in the corpus). So extraction is named-entity work: map "nvidia" → NVDA, "google" → GOOGL, etc. Each subagent IS the LLM doing this — no external API.

## Config
```
INPUT:        ~/Downloads/YoutubeSummaries/transcripts.jsonl   (--input / first positional)
CONCURRENCY:  30        (--concurrency)
OUTPUT DIR:   ~/Downloads/YoutubeSummaries/                    (-o)
RUN DIR:      /tmp/ticker-run-<timestamp>/                     (chunks + per-agent extract files)
TOP:          25        (--top: how many rows to show per leaderboard)
```

## Step 1: Split the corpus (app — `agent-reach split-corpus`)

Don't hand-roll this — the app does it (tested). Pick a fresh `RUN_DIR=/tmp/ticker-run-<timestamp>`, then:
```bash
agent-reach split-corpus --input <INPUT.jsonl> --out-dir <RUN_DIR> --chunks <CONCURRENCY>
```
It keeps `status=="ok"` records with a non-empty transcript, writes `<RUN_DIR>/authors.json` (`total_authors`, `videos_per_author` — the normalization denominator — and `N`), and splits into `chunk_01.jsonl … chunk_<A>.jsonl` (`A = min(--chunks, N)`, full records per chunk). Surface its printed `N` / authors / chunk count to the user.

## Step 3: Fan out the extraction team (single concurrent wave)

Spawn **all `A` subagents in ONE message** (`subagent_type: general-purpose`). Each agent's prompt includes its `chunk_NN.jsonl` path, its `extract_NN.jsonl` output path, and these instructions:

> Read every record in `<chunk_NN.jsonl>`. For each video's transcript, identify every **publicly-investable** ticker discussed (stocks, ETFs, major crypto). For each (video, ticker) emit ONE JSON line to `<extract_NN.jsonl>`:
> `{"video_id":…,"channel":…,"ticker":"<OFFICIAL UPPERCASE SYMBOL>","company":"…","asset_type":"stock|etf|crypto|index","mentions":<approx count in this video>,"sentiment":"bullish|bearish|neutral","conviction":<1-5>,"is_primary_topic":<bool>,"recommendation":"strong_buy|buy|hold|sell|strong_sell|watch|none","thesis":"<≤240-char summary of WHY they hold this stance — the actual argument, not a description>","catalyst":"<specific driver/event they cite, or null>","time_horizon":"short|medium|long|unspecified","price_target":<string or null>,"quote":"<≤200-char representative quote>"}`
> Rules: resolve the official ticker for well-known names (nvidia→NVDA, google→GOOGL, meta→META, amd→AMD, bitcoin→BTC); only use `ticker:null` if genuinely unresolvable. De-dupe aliases within a video to ONE row (sum the mentions). `is_primary_topic` = the video is substantially ABOUT this ticker, not a name-drop in a list. `sentiment` = the author's stance on the ticker in THIS video. `conviction`: 5 = "my #1 pick / loading up / table-pounding", 1 = passing mention. **`thesis` is the most important field** — capture the author's actual reasoning; if they give NO reasoning (pure name-drop), set `thesis:null` and `conviction:1`. `catalyst` = the concrete reason it moves (earnings, product, Fed, regulation…), null if none stated. Skip generic market talk with no specific ticker. Do NOT summarize transcripts.
> Then return ONLY a compact JSON object: `{"chunk":NN,"videos_attempted":<n>,"videos_done":<n>,"ticker_rows":<n>,"videos_with_no_ticker":[<video_id…>],"skipped":[{"video_id":…,"reason":…}],"output_file":"<path>"}`

## Step 4: Collect + retry dead agents (once)

Gather each agent's JSON. A chunk is **dead** if the agent crashed/returned null/reported an `agent_error`, or `extract_NN.jsonl` is missing, or `videos_done < videos_attempted`. Re-spawn each dead chunk **once**; if it still fails, record its video_ids as **unprocessed** and continue — never block on one chunk.

## Step 5: Grade + integrity + markdown (app — `agent-reach grade`)

The app does grading, the integrity check, AND the markdown render — one call, no ad-hoc scripts:
```bash
agent-reach grade --extract-dir <RUN_DIR> --authors <RUN_DIR>/authors.json -o ~/Downloads/YoutubeSummaries/tickers-<timestamp>.json
```
It loads every `extract_*.jsonl` + `authors.json`, applies the formula below, and writes **three things**: the graded `.json`, a sibling `.md` leaderboard (same path, `.md` extension), and a printed+embedded **Integrity** line (`corpus N / covered / with_tickers / missing [OK|FAIL]`) reconciled against the `chunk_*.jsonl` it finds in `RUN_DIR`. Do NOT re-implement grading, integrity, or the markdown in an inline script — the formula is documented here only so the ranking is auditable:

**Per mention** (one extract row), first compute a **context score** `ctx ∈ 0–1` — this is what makes substance beat repetition:
- start at 0; `+0.45` if `thesis` is non-null and substantive, `+0.20` if `catalyst` non-null, `+0.15` if `price_target` non-null, `+0.10` if `is_primary_topic`, `+0.10` if `time_horizon != "unspecified"`. Clamp to 1.0.
- A pure name-drop (no thesis/catalyst/target) scores ~0 and contributes almost nothing, no matter how many times it recurs.

**Per author `a` that mentions T:**
- `m_a` = number of DISTINCT videos by `a` mentioning T  *(repeated mentions inside one video count once — within-author saturation)*
- `coverage_a = m_a / videos_per_author[a]`  *(0–1 — normalizes away channel volume; non-negotiable)*
- `ctx_a` = mean `ctx` over a's mentions of T  *(how well-reasoned a's discussion is — the context signal)*
- `conv_a` = mean conviction of a's mentions, scaled to 0–1 (`/5`)
- `sent_a` = mean of {bullish:+1, neutral:0, bearish:−1} over a's mentions
- `strength_a = coverage_a * (0.25 + 0.75*ctx_a) * (0.5 + 0.5*conv_a)`   *(context is the dominant multiplier — a mention with no reasoning keeps only 25% of its weight even at full conviction)*

**Across authors:**
- `bull_authors` = authors with `sent_a > 0`; `bear_authors` = authors with `sent_a < 0`
- `bullish_score = len(bull_authors) * Σ_{a∈bull} strength_a`   *(breadth × summed strength — consensus dominates repetition)*
- `bearish_score = len(bear_authors) * Σ_{a∈bear} strength_a`
- `breadth = #distinct authors mentioning T` (shown as `breadth/total_authors`)
- `contested = (len(bull_authors) ≥ 1 and len(bear_authors) ≥ 1)`

Build two leaderboards: **Bullish** (sorted by `bullish_score` desc), **Bearish** (by `bearish_score` desc). A contested ticker can appear on both, flagged. Normalize each board's scores to 0–100 (relative to its max) and tier them: **S ≥ 80, A ≥ 60, B ≥ 40, C < 40**.

Per-ticker output fields: `ticker, company, asset_type, tier, score, breadth("X/total"), authors[], n_videos_mentioning, net_sentiment, avg_conviction, avg_context, contested, top_theses[(channel, thesis)…], catalysts[], price_targets[], sample_quote, video_ids[]`. Carry the strongest theses/catalysts through so the rank is auditable — the user can see *why* a ticker ranks, not just that it does.

> The weights (0.5/0.5/0.25, the breadth multiplier, tier cutoffs) are intentionally explicit and tunable — keep them in one place at the top of the grading script so they're easy to adjust.

## Step 6: Verify (the app already did the integrity math)

`agent-reach grade` printed and embedded the Integrity line. Confirm it reads `OK` (`missing=0`). If it reports `FAIL`/`missing>0`, say so loudly — some videos from dead chunks never produced extracts; re-run those chunks (Step 4) before presenting. A video that legitimately produced zero tickers (macro/no specific ticker) is still `covered`, not missing. Do not present a clean leaderboard over a `FAIL`.

## Step 7: Present

The `.json` and `.md` are already written by Step 5. Read the `.json` and present to the user:
- **Bullish top `TOP`** and **Bearish top `TOP`** as compact tables: `rank | ticker | company | tier | score | breadth(X/total) | context | one-line thesis`
- For the top few of each board, show the **strongest thesis + catalyst** (with the channel) so the ranking is justified by context, not just a number.
- A short **Contested** callout (authors disagree) — these are the interesting ones; show both the bull and bear thesis.
- One line confirming integrity ("all N videos accounted for") or the failure.
- Offer next: deeper dive on a ticker (pull its quotes across videos), per-channel breakdown, or re-grade with different weights.

## Notes
- Output (leaderboards + graded JSON) → `~/Downloads/YoutubeSummaries/`. Chunks + per-agent extract files stay in the `/tmp/` run dir.
- Extraction is the only LLM step; grading is deterministic, so re-running the grade with tweaked weights is cheap (reuse `extract_*.jsonl`, skip Step 3).
- Pairs with `/yt-transcripts` (which produces the input JSONL). Recency weighting is intentionally out of v1 (needs upload_date, not currently stored).
