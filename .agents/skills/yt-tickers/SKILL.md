---
name: yt-tickers
description: Extract stock/crypto tickers from a corpus of YouTube transcripts (JSONL) in parallel via a team of subagents, then grade them into ranked Bullish/Bearish leaderboards using cross-author consensus. Auto-triggers when user says 'ticker extract', 'extract tickers', 'grade tickers', 'which stocks are they talking about', 'ticker leaderboard', or similar, pointed at a transcripts file. Arguments: Path to a transcripts JSONL (default: the newest transcripts-*.jsonl in "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries"). Optional: '--concurrency <n>' (default 30), '-o <dir>' output dir, '--top <n>' leaderboard size.
allowed-tools: Bash, Read, Write, Grep, Glob, Agent, Task
---

# Ticker Extract & Grade: $ARGUMENTS

You are the **orchestrator**. From a corpus of YouTube transcripts, extract every investable ticker discussed, then grade them into **two leaderboards (Bullish / Bearish)** ranked primarily by *cross-author consensus* **weighted by the quality of the conversational context** around each mention. The principle: **extraction is LLM work (done by subagents), grading is deterministic math (done by you in a Python step)** — so the ranking is reproducible, not vibes.

**Context is the key signal.** A bare name-drop is nearly worthless; a reasoned thesis with a catalyst and a price target is gold. Two authors who actually *argue* a position must be able to outrank four authors who merely mention the ticker in passing. So extraction must capture the *reasoning* (thesis, catalyst, recommendation), and grading multiplies each mention's weight by how substantive its context is — raw mention counts never drive the rank on their own.

Tickers appear in transcripts as **spoken company names**, not `$SYMBOL` (verified: zero `$TICKER` hits in the corpus). So extraction is named-entity work: map "nvidia" → NVDA, "google" → GOOGL, etc. Each subagent IS the LLM doing this — no external API.

## Config
```
INPUT:        "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/transcripts-<timestamp>.jsonl"   (--input / first positional; default the newest one)
CONCURRENCY:  30        (--concurrency)
OUTPUT DIR:   "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/"   (-o; keep it quoted: the name has a space)
RUN DIR:      /tmp/ticker-run-<timestamp>/                     (chunks + per-agent extract files)
TOP:          25        (--top: how many rows to show per leaderboard)
```

## Step 1: Split the corpus (app — `agent-reach stockyt split-corpus`)

Don't hand-roll this — the app does it (tested). Pick a fresh `RUN_DIR=/tmp/ticker-run-<timestamp>`, then:
```bash
agent-reach stockyt split-corpus --input "<INPUT.jsonl>" --out-dir <RUN_DIR> --chunks <CONCURRENCY>
```
It keeps `status=="ok"` records with a non-empty transcript, writes `<RUN_DIR>/authors.json` (`total_authors`, `videos_per_author` — the normalization denominator — and `N`), and splits into `chunk_01.jsonl … chunk_<A>.jsonl` (`A = min(--chunks, N)`, full records per chunk). Surface its printed `N` / authors / chunk count to the user.

## Step 3: Fan out the extraction team (single concurrent wave)

Spawn **all `A` subagents in ONE message** (`subagent_type: general-purpose`, `name: extract-NN` — Step 5 continues the same agent by that name). Each agent's prompt includes its `chunk_NN.jsonl` path, its `extract_NN.jsonl` output path, and these instructions:

> Read every record in `<chunk_NN.jsonl>`. For each video's transcript, identify every **publicly-investable** ticker discussed (stocks, ETFs, major crypto). For each (video, ticker) emit ONE JSON line to `<extract_NN.jsonl>`:
> `{"video_id":…,"channel":…,"ticker":"<OFFICIAL UPPERCASE SYMBOL>","company":"…","asset_type":"stock|etf|crypto|index","mentions":<approx count in this video>,"sentiment":"bullish|bearish|neutral","conviction":<1-5>,"is_primary_topic":<bool>,"recommendation":"strong_buy|buy|hold|sell|strong_sell|watch|none","thesis":"<≤240-char summary of WHY they hold this stance — the actual argument, not a description>","catalyst":"<specific driver/event they cite, or null>","time_horizon":"short|medium|long|unspecified","price_target":<string or null>,"quote":"<≤200-char representative quote>"}`
> Rules: resolve the official ticker for well-known names (nvidia→NVDA, google→GOOGL, meta→META, amd→AMD, bitcoin→BTC); only use `ticker:null` if genuinely unresolvable. De-dupe aliases within a video to ONE row (sum the mentions). `is_primary_topic` = the video is substantially ABOUT this ticker, not a name-drop in a list. `sentiment` = the author's stance on the ticker in THIS video. `conviction`: 5 = "my #1 pick / loading up / table-pounding", 1 = passing mention. **`thesis` is the most important field** — capture the author's actual reasoning; if they give NO reasoning (pure name-drop), set `thesis:null` and `conviction:1`. `catalyst` = the concrete reason it moves (earnings, product, Fed, regulation…), null if none stated. Skip generic market talk with no specific ticker. Do NOT summarize transcripts.
> Then return ONLY a compact JSON object: `{"chunk":NN,"videos_attempted":<n>,"videos_done":<n>,"ticker_rows":<n>,"videos_with_no_ticker":[<video_id…>],"skipped":[{"video_id":…,"reason":…}],"output_file":"<path>"}`

## Step 4: Collect + retry dead agents (once)

Gather each agent's JSON. A chunk is **dead** if the agent crashed/returned null/reported an `agent_error`, or `extract_NN.jsonl` is missing, or `videos_done < videos_attempted`. Re-spawn each dead chunk **once**; if it still fails, record its video_ids as **unprocessed** and continue — never block on one chunk.

## Step 5: Review (two rounds — agents judge, the app decides)

A second agent checks every chunk's extract: **(a)** were the right tickers picked, **(b)** is the sentiment what the transcript says. Exactly **two rounds**, always — never more, never fewer. The loop is modelled on `~/.agents/skills/handoff-review/SKILL.md` (reviewer finds, implementor answers, reviewer re-checks), but here nobody edits `extract_NN.jsonl` and no agent labels a row: every agent only writes its own JSON file, and `agent-reach stockyt review-apply` decides each row's outcome from those files.

Per chunk `NN`, all files in `<RUN_DIR>`:

| File | Written by | Shape |
|---|---|---|
| `review_NN_r1.json` | `review-NN` | `[{"id":"f1","video_id":…,"ticker":…,"issue":…,"proposed":{…},"quote":"…"}]` |
| `response_NN_r1.json` | `extract-NN` | `[{"id":"f1","decision":"accept\|reject","quote":"…","reason":"…"}]` — one per R1 finding |
| `review_NN_r2.json` | `review-NN` | `{"verdicts":[{"id":"f1","verdict":"hold\|withdraw"}],"findings":[…r2- findings…]}` |
| `response_NN_r2.json` | `extract-NN` | same shape as R1 response, one per `r2-` finding |

Finding fields:
- `issue` is one of `wrong_ticker` · `wrong_sentiment` · `not_discussed` (the video never really discusses it) · `not_investable` (not a publicly-investable stock/ETF/crypto/index) · `missed_ticker` (a discussed ticker with no row).
- `video_id` + `ticker` name the extract row the finding is about (`ticker` is `null` for `missed_ticker`).
- `proposed` is `{"ticker","company","asset_type"}` for `wrong_ticker`, `{"sentiment"}` for `wrong_sentiment`, a **full extract row** (same schema as Step 3, including `video_id` and `channel`) for `missed_ticker`, and `{}` otherwise.
- `quote` is **required**: verbatim words from the transcript that support the finding.

Every agent writes its file **even when it has nothing to say** (`[]`, or `{"verdicts":[],"findings":[]}`). A missing file fails that chunk in `review-apply`.

**Round 1 — review.** Spawn all `A` reviewers in ONE message (`subagent_type: general-purpose`, `name: review-NN`). Prompt:

> You are reviewing another agent's ticker extraction. Read every record in `<chunk_NN.jsonl>` (the transcripts) and every row in `<extract_NN.jsonl>` (the extraction). Check two things for each video: (a) the tickers — is each row's ticker the right official symbol for what was discussed, was it really discussed, is it publicly investable, and is any discussed ticker missing; (b) the sentiment — is each row's `sentiment` the author's actual stance in THIS video. Report only real errors you can prove with a verbatim quote from the transcript; do not report style, thesis wording, conviction or mention counts. Write a JSON array of findings to `<RUN_DIR>/review_NN_r1.json` using ids `f1`, `f2`, … and exactly these fields: `id, video_id, ticker, issue, proposed, quote` (schema below). Write `[]` if everything holds. Do not edit any other file. Then return only `{"chunk":NN,"findings":<n>,"output_file":"<path>"}`.
> <paste the "Finding fields" list above>

**Round 1 — response.** In ONE message, `SendMessage` each `extract-NN`:

> A reviewer checked your extraction. Read `<RUN_DIR>/review_NN_r1.json` and the transcripts in `<chunk_NN.jsonl>` again. For every finding decide `accept` (the reviewer is right) or `reject` (you are right; give a verbatim transcript `quote` that shows it). Write `<RUN_DIR>/response_NN_r1.json`: a JSON array with exactly one `{"id","decision","quote","reason"}` per finding (`[]` if there were none). Do NOT edit `extract_NN.jsonl` — the app applies accepted findings. Return only `{"chunk":NN,"accepted":<n>,"rejected":<n>}`.

**Round 2 — review.** In ONE message, `SendMessage` each `review-NN`:

> The extractor answered your findings in `<RUN_DIR>/response_NN_r1.json`. Write `<RUN_DIR>/review_NN_r2.json` as `{"verdicts":[…],"findings":[…]}`. `verdicts`: exactly one `{"id","verdict"}` for every finding the extractor rejected — `withdraw` if their quote convinces you, else `hold`. `findings`: new findings only about rows your accepted findings changed (the corrected ticker or sentiment, or a row you proposed as `missed_ticker`), same fields as before, ids `r2-1`, `r2-2`, …; usually `[]`. Return only `{"chunk":NN,"held":<n>,"withdrawn":<n>,"new_findings":<n>}`.

**Round 2 — response.** In ONE message, `SendMessage` each `extract-NN`: answer the `r2-` findings in `review_NN_r2.json` exactly as in round 1, into `<RUN_DIR>/response_NN_r2.json` (`[]` if there are none). This is the last round: there is no round 3.

If `SendMessage` cannot reach an agent (it ended, or the runtime cannot continue agents), spawn a fresh agent with the same `name` and the same prompt plus the files written so far; it starts from the files, not from memory.

**Dead agents.** A review or response agent that crashed or did not write its file is re-spawned **once**. If it fails again, record the chunk as **unreviewed** and carry on with the others — `review-apply` will fail that chunk loudly.

**Apply (app).**
```bash
agent-reach stockyt review-apply --run-dir <RUN_DIR>
```
It writes `reviewed_NN.jsonl` per chunk and `review_summary.json`, and prints the counts (`confirmed · corrected · added · dropped · disputed`, plus disputed missed tickers) and `Review: OK|FAIL`. Each reviewed row keeps the extract fields and adds `review_status` (`confirmed | corrected | added | dropped | disputed`) and `review_note` (the finding ids that touched it); a corrected row also keeps `original_ticker` / `original_sentiment`. How it decides:
- accepted `wrong_ticker` / `wrong_sentiment` → the row is corrected; accepted `not_discussed` / `not_investable` → dropped; accepted `missed_ticker` → a new row is added.
- rejected in R1 and **held** in R2, or an `r2-` finding rejected → **disputed**. The two agents could not agree, so the row moves neither leaderboard; it is only counted.
- rejected and withdrawn, or no finding → confirmed.

On `Review: FAIL` it lists each problem (a finding with no response, a rejected finding with no verdict, a finding about a row that does not exist, a missing file). Fix the cause — usually re-run that chunk's round — and re-run `review-apply`. **Do not grade over a FAIL.** If a chunk stays unreviewed after its retry, stop and tell the user which chunks and why.

## Step 6: Grade + integrity + markdown (app — `agent-reach stockyt grade --reviewed`)

The app does grading, the integrity check, AND the markdown render — one call, no ad-hoc scripts:
```bash
agent-reach stockyt grade --reviewed --extract-dir <RUN_DIR> --authors <RUN_DIR>/authors.json -o "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/tickers-<timestamp>.json"
```
With `--reviewed` it grades `reviewed_*.jsonl` (refusing if `review_summary.json` is not OK or any `extract_NN.jsonl` has no `reviewed_NN.jsonl`), leaves out dropped and disputed rows, and adds a review line and a `Disputed` column to the markdown. It loads every reviewed row + `authors.json`, applies the formula below, and writes **three things**: the graded `.json`, a sibling `.md` leaderboard (same path, `.md` extension), and a printed+embedded **Integrity** line (`corpus N / covered / with_tickers / missing [OK|FAIL]`) reconciled against the `chunk_*.jsonl` it finds in `RUN_DIR`. Do NOT re-implement grading, integrity, or the markdown in an inline script — the formula is documented here only so the ranking is auditable:

**Per mention** (one extract row), first compute a **context score** `ctx ∈ 0–1` — this is what makes substance beat repetition:
- start at 0; `+0.45` if `thesis` is non-null and substantive, `+0.20` if `catalyst` non-null, `+0.15` if `price_target` non-null, `+0.10` if `is_primary_topic`, `+0.10` if `time_horizon != "unspecified"`. Clamp to 1.0.
- A pure name-drop (no thesis/catalyst/target) scores ~0 and contributes almost nothing, no matter how many times it recurs.

**Per author `a` that mentions T, on each side** (bullish, then bearish, computed separately):
- `m_a` = number of DISTINCT videos by `a` whose row for T has that side's sentiment  *(repeated mentions inside one video count once — within-author saturation; neutral rows count on neither side)*
- `coverage_a = m_a / (videos_per_author[a] + K)`, `K = 1`  *(normalizes away channel volume; K keeps a one-video channel from scoring full coverage)*
- `ctx_a` = mean `ctx` over a's rows of that side  *(how well-reasoned a's case is — the context signal)*
- `conv_a` = mean conviction of a's rows of that side, scaled to 0–1 (`/5`)
- `strength_a = coverage_a * (0.25 + 0.75*ctx_a) * (0.5 + 0.5*conv_a)`   *(context is the dominant multiplier — a mention with no reasoning keeps only 25% of its weight even at full conviction)*
- `sent_a` = mean of {bullish:+1, neutral:0, bearish:−1} over all a's rows (only for the contested flag)

**Across authors:**
- `bull_authors` = authors with at least one bullish video on T; `bear_authors` = at least one bearish video
- `bullish_score = len(bull_authors) * Σ_{a∈bull} strength_a(bullish)`   *(breadth × summed strength — consensus dominates repetition)*
- `bearish_score = len(bear_authors) * Σ_{a∈bear} strength_a(bearish)`
- `breadth = #distinct authors mentioning T`; each board shows its side's count next to it (`2 bull · 3/5`)
- `contested = some author has sent_a > 0 and another has sent_a < 0`

Build two leaderboards: **Bullish** (sorted by `bullish_score` desc), **Bearish** (by `bearish_score` desc). A ticker can appear on both; it is flagged contested when channels disagree on net. Normalize each board's scores to 0–100 (relative to its max) and tier them: **S ≥ 80, A ≥ 60, B ≥ 40, C < 40**.

Per-ticker output fields: `ticker, company, asset_type, tier, score, breadth("X/total"), authors[], n_videos_mentioning, net_sentiment, avg_conviction, avg_context, contested, bull_breadth, bear_breadth, top_theses{bullish:[(channel, thesis)…], bearish:[…]} (each sorted by ctx × conviction; each board shows its own side's first), catalysts[], price_targets[], sample_quote, video_ids[]`. Carry the strongest theses/catalysts through so the rank is auditable — the user can see *why* a ticker ranks, not just that it does.

> The weights (0.5/0.5/0.25, the breadth multiplier, tier cutoffs) are intentionally explicit and tunable — keep them in one place at the top of the grading script so they're easy to adjust.

## Step 7: Verify (the app already did the integrity math)

`agent-reach stockyt grade` printed and embedded the Integrity line. Confirm it reads `OK` (`missing=0`). If it reports `FAIL`/`missing>0`, say so loudly — some videos from dead chunks never produced extracts; re-run those chunks (Step 4) before presenting. A video that legitimately produced zero tickers (macro/no specific ticker) is still `covered`, not missing. Do not present a clean leaderboard over a `FAIL`.

## Step 8: Present

The `.json` and `.md` are already written by Step 6. Read the `.json` and present to the user:
- **Bullish top `TOP`** and **Bearish top `TOP`** as compact tables: `rank | ticker | company | tier | score | breadth(X/total) | context | one-line thesis`
- For the top few of each board, show the **strongest thesis + catalyst** (with the channel) so the ranking is justified by context, not just a number.
- A short **Contested** callout (authors disagree) — these are the interesting ones; show both the bull and bear thesis.
- One line confirming integrity ("all N videos accounted for") or the failure.
- **What the review changed**: the counts from the `review` block of the `.json` (`confirmed`, `corrected`, `added`, `dropped`, `disputed`, `disputed_missed`), then, for the top tickers of each board, every row whose `review_status` is `corrected` or `disputed` (from `reviewed_*.jsonl`): channel, `original_*` → new value or the disputed issue, and the quote. Disputed-only tickers are in the `.json`'s `disputed` map.
- Offer next: deeper dive on a ticker (pull its quotes across videos), per-channel breakdown, or re-grade with different weights.

## Notes
- Output (leaderboards + graded JSON) → "$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries/". Chunks + per-agent extract files stay in the `/tmp/` run dir.
- Extraction and the review are the only LLM steps; review outcomes and grading are deterministic, so re-running the grade with tweaked weights is cheap (reuse `reviewed_*.jsonl`, skip Steps 3–5).
- Pairs with `/yt-transcripts` (which produces the input JSONL). Recency weighting is intentionally out of v1 (needs upload_date, not currently stored).
