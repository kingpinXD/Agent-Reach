# stockyt

stockyt reads what a fixed set of finance YouTube channels said over a window of days and ranks
the tickers they talk about. It writes two boards: tickers the channels are bullish on, and tickers
they are bearish on.

It is a personal project inside a personal fork of Agent-Reach (`kingpinXD/Agent-Reach`). It lives
under `agent-reach stockyt` so an upstream merge never touches it. Every call into upstream code
goes through one file, `agent_reach/stockyt/upstream.py`.

One command runs everything:

```
channels → transcripts → corpus → extract → review → grade → report
```

Code does every step that can be written as logic. An LLM does two things only: read transcripts
for tickers and sentiment (extract), and check that reading (review).

## Install

Use a venv in the clone, or pipx:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # then run .venv/bin/agent-reach
pipx install -e <path to the clone>                          # then run agent-reach
```

The `agent-reach` you run must be a build of this branch. An old build says `stockyt` is an unknown
command, or lacks `run`.

yt-dlp comes with the install, pinned in `pyproject.toml`. Do not use another yt-dlp (see
Troubleshooting).

## Commands

```bash
agent-reach stockyt doctor
agent-reach stockyt run --since 20261105T1600 --label postcrash
agent-reach stockyt run --resume postcrash
agent-reach stockyt status postcrash
agent-reach stockyt regrade postcrash
```

**`doctor`** prints one line per check: the yt-dlp version, a JS runtime (deno or node), whether the
output folder is writable, and whether `claude -p` answers (claude-cli backend only). It also prints
how fresh each channel's cache is, as information. Any `FAIL` exits 1.

**`run`** starts a run. It runs the doctor checks first and stops on a `FAIL`.
- `--since` is a day (`YYYYMMDD`) or a moment (`YYYYMMDDTHHMM`). A moment is read in the configured
  timezone, US Eastern by default.
- `--until YYYYMMDD` ends the window on that day, inclusive. Without it, the window runs to today.
- `--label` names the run's folder.
- `--llm claude-cli|session|fake` picks the LLM backend (see below).
- `--cookies-from-browser <browser>` sends that browser's YouTube cookies. Only use it with the
  user's OK.

**`run --resume <label>`** continues a run where it stopped. A stage that finished, and whose inputs
did not change, is skipped. `--cookies-from-browser` and `--llm` can be given again here; the run
keeps them for later resumes. `--retry-failed` asks the LLM tasks that stayed invalid after their
re-ask again.

**`status <label>`** prints each stage's status and check lines, and any prompt files still waiting
for an answer.

**`regrade <label>`** grades a finished run again with the weights in today's config file and
rewrites its report. It records the new weights in the run's `run.json`. Nothing is fetched or asked
again.

The single-step commands still work on their own files: `channels`, `transcripts`, `split-corpus`,
`review-apply` and `grade`. `run` does not need them.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | Done | Read `report.md` |
| 1 | A check failed | The printed `[stage]` lines say which check and why |
| 2 | Blocked on the user | YouTube is rate limiting. Ask the user, then run the printed `--resume <label> --cookies-from-browser chrome` command |
| 3 | Waiting on LLM tasks | Session backend only. Answer each printed prompt file, then `--resume` |

## Config

`~/.config/stockyt/config.toml` sits over the code defaults. Every key is optional. A key the
program does not know is an error.

| Key | Default | Meaning |
|---|---|---|
| `output_dir` | `~/Documents/Claude/Cowork Projects/YoutubeSummaries` | Runs and the channel cache live here |
| `timezone` | `America/New_York` | How a `--since` time of day is read |
| `channels` | the 11 channels in `config.py` | List of `["Name", "channel id"]` pairs |
| `lang` | `en` | Caption language. `<lang>-orig` is preferred over `<lang>` |
| `whisper` | `false` | Fall back to Whisper speech-to-text for a video with no captions |
| `pace_seconds` | `5.0` | Pause between yt-dlp calls |
| `cookies_from_browser` | none | Send this browser's cookies on every run. Leave it unset; pass the flag per run instead |
| `llm_backend` | `claude-cli` | `claude-cli`, `session` or `fake` |
| `llm_model` | none | Model for `claude -p`. None means the CLI's default |
| `llm_workers` | `4` | LLM calls at the same time |
| `chunk_chars` | `60000` | Transcript characters per extraction call. The number of chunks follows from it |

Grading weights go in a `[weights]` table:

| Key | Default | Meaning |
|---|---|---|
| `thesis` | `0.45` | Context score for a real thesis |
| `catalyst` | `0.20` | … for a named catalyst |
| `price_target` | `0.15` | … for a price target |
| `primary` | `0.10` | … when the video is about this ticker |
| `horizon` | `0.10` | … when a time horizon is given |
| `coverage_smoothing_k` | `1.0` | K in the coverage formula below |

Each run keeps a copy of the settings in `run.json`, so a resume uses the settings the run started
with.

## The run folder

Every file of a run is in `OUTPUT_DIR/runs/<label>/`:

```
run.json                 each stage's status, attempts, input hash and check lines; params; settings copy
log.jsonl                one line per outside call (yt-dlp, LLM)
.lock                    present while a process works on the run
links.txt, channels.json         channels
transcripts.jsonl                transcripts
chunks/authors.json, chunk_NN.jsonl                        corpus
chunks/extract_NN.jsonl, extract_NN.videos.json             extract
chunks/review_NN_r1.json, response_NN_r1.json,
       review_NN_r2.json, response_NN_r2.json,
       reviewed_NN.jsonl, review_summary.json                review
graded.json                      grade
tickers.json, report.md          report
llm/cache/               valid LLM answers, keyed by prompt, schema and model
llm/session/             prompt and answer files of the session backend
```

`report.md` is the deliverable. It has the two boards and a "What the review changed" table.
`tickers.json` holds the same data plus `review_changes`.

Every write goes to a temp file first and is then renamed into place. The channel cache is
`OUTPUT_DIR/.cache/channels/`. It is only used when a channel's listing fails.

## What each stage checks

- **channels** fails when a channel's listing fails, or ends before the window start.
- **transcripts** fetches one video at a time. A rate limit stops it as blocked (exit 2). A link with
  no row fails it. A video without captions is an error row, not a failure.
- **corpus** keeps one good transcript per video. It fails when no video has one.
- **extract** fails unless every video is marked done or no-tickers: `Extract: X/N videos [OK]`.
- **review** fails unless the review files apply cleanly: `Review: OK`.

Every LLM answer is checked: its shape, the ids, that the row exists, and that each quote is really
in the transcript. An invalid answer is asked once more with the errors listed. If it is still
invalid, the task is unrecovered: the run exits 1 naming it, and stays that way until
`--resume <label> --retry-failed`.

## LLM backends

- **`claude-cli`** (default) runs `claude -p` once per task, on the user's existing Claude login. It
  needs no session, so `run` works from a plain terminal. The flags keep the user's CLAUDE.md, hooks,
  MCP servers and tools out of each call, and stop it saving a session.
- **`session`** writes a prompt file per task and exits 3. A Claude session answers each file (the
  `yt-stocks` skill does this), then resumes the run.
- **`fake`** is for tests. Its answers come from code.

## The review

The review has at most two rounds, per chunk:
1. A reviewer lists findings, each with a quote from the transcript.
2. The extractor accepts or rejects each finding.
3. In round 2, the reviewer holds or withdraws each rejected finding. It may add findings about rows
   its accepted findings changed or added. The extractor answers those.

A call with nothing to answer is skipped, and the program writes its empty result. So there is no
round-1 response when round 1 found nothing. There is no round 2 unless a finding was rejected, or an
accepted one changed or added a row.

The program, not an agent, decides each row: confirmed, corrected, added, dropped or disputed. A
finding still rejected after round 2 makes its row disputed. Disputed and dropped rows are left out
of the scores.

## How ranking works

For each ticker, each channel and each side (bullish or bearish):
- **coverage** = videos taking that side ÷ (the channel's videos in the window + K). K is
  `coverage_smoothing_k`. It stops a channel with one video from scoring full coverage.
- **context** = the mean context score of those rows. A row scores the weights above for each part
  it has, capped at 1. A thesis counts only when it is real prose.
- **conviction** = the mean conviction of those rows, scaled to 0–1.
- **strength** = coverage × (0.25 + 0.75 × context) × (0.5 + 0.5 × conviction).

A ticker's side score is the number of channels on that side times the sum of their strengths. Each
board is scaled so its top ticker is 100. Tiers: S from 80, A from 60, B from 40, C below.

A neutral row counts for neither side. A ticker can be on both boards. It is **contested** when one
channel is net bullish and another net bearish. Breadth reads `2 bull · 3/5`: two channels bullish,
three of five channels mention it. Each board shows its own side's strongest thesis.

## Troubleshooting

**`stockyt` or `run` is an unknown command.** The `agent-reach` on PATH is an old build. Use this
branch's venv (`.venv/bin/agent-reach`) or reinstall with pipx.

**Every upload date is `NA`, or `channels` says "no usable rows".** Another yt-dlp is running.
Homebrew's yt-dlp 2026.06.09 returns `NA` dates. The program runs the pinned one
(`python -m yt_dlp`); `doctor` prints its version.

**Almost every transcript is an error.** yt-dlp needs a JS runtime for YouTube. Put `deno` or `node`
on PATH; `doctor` checks it.

**Exit 2: YouTube is rate limiting.** Many caption calls in a row get HTTP 429, and the block lasts
hours. Waiting did not clear it before. Browser cookies did, at once. Ask the user before sending
them.

**A video has no captions.** It becomes an error row and is skipped; the run still completes. With
`whisper = true` the program tries speech-to-text. That needs `ffmpeg` and a Groq or OpenAI key
(`agent-reach configure groq-key`).

**The listing date can be a day late.** yt-dlp's listing date is the real upload date or the next
day. The program checks the real date of each video near the window's edges, so the window is right.

**`run` says the run is locked.** Another stockyt process is working on it. If none is, delete the
`.lock` file the message names.

## Keeping the fork in sync

The fork tracks `Panniantong/Agent-Reach` (git remote `upstream`). To sync:
`git fetch upstream && git merge upstream/main`, then run the tests. stockyt's code is in
`agent_reach/stockyt/` and the one `stockyt` line in `agent_reach/runtime/cli.py`. If a sync breaks
stockyt, the fix belongs in `agent_reach/stockyt/upstream.py`.
