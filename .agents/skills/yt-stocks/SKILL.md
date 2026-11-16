---
name: yt-stocks
description: Rank the stock tickers the finance YouTube channels talked about since a day or moment, with `agent-reach stockyt run`. Auto-triggers when the user says 'yt stocks', 'stock yt', 'ticker leaderboard', 'what are the channels bullish on', 'run stockyt', or similar. Arguments: a window start as YYYYMMDD or YYYYMMDDTHHMM (US Eastern) and a run label; or 'resume <label>'. If the start is missing, ask for it.
allowed-tools: Bash, Read, Write, Agent
---

# yt-stocks: $ARGUMENTS

The program does every step; this skill only runs it and acts on its exit code. Manual: `docs/stockyt/README.md`.

1. Run `agent-reach stockyt run --since <start> --label <label> --llm session` (or `run --resume <label>`). Use the worktree's `.venv/bin/agent-reach` if `agent-reach stockyt --help` lacks `run`. Without `--llm session`, the run calls `claude -p` itself and needs nothing from this skill.
   For Indian channels, add `--market india` to a new run (the start is then Indian time). A resume keeps the run's market.
2. Act on the exit code:
   - **0**: go to step 3.
   - **1**: a check failed. Show the `[stage]` lines to the user and stop. Never work around a FAIL.
   - **2**: YouTube is rate limiting. Ask the user whether to send Chrome's cookies. Only on a yes, run the printed `--resume <label> --cookies-from-browser chrome` command, then go to step 2.
   - **3**: spawn one agent per printed prompt file, all at once. Tell each: "Read <prompt file> and do exactly what it says; write only the answer file it names." When all are done, run `agent-reach stockyt run --resume <label>` and go to step 2.
3. Show `report.md` from the run folder (the last printed line names it). Summarise it in a few lines: the top bullish and bearish tickers, contested ones, and what the review changed.
