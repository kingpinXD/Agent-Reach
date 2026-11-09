# Runbook: YouTube finance channels → stock ticker leaderboards

Everything about stockyt is in [`docs/stockyt/README.md`](stockyt/README.md): install, commands,
exit codes, config, the run folder, LLM backends, how ranking works, and troubleshooting.

## A run, start to finish

```bash
agent-reach stockyt doctor                                      # every line OK
agent-reach stockyt run --since <YYYYMMDD[THHMM]> --label <name>
```

Then by exit code:
- **0** — done. Open `OUTPUT_DIR/runs/<name>/report.md`.
- **1** — a check failed. Read the `[stage]` lines, fix the cause, then
  `agent-reach stockyt run --resume <name>`. For unrecovered LLM tasks add `--retry-failed`.
- **2** — YouTube is rate limiting. Ask the user before sending browser cookies, then
  `agent-reach stockyt run --resume <name> --cookies-from-browser chrome`.
- **3** — session backend only: answer each printed prompt file, then
  `agent-reach stockyt run --resume <name>`. The `yt-stocks` skill does this.

`agent-reach stockyt status <name>` shows where a run stands. `agent-reach stockyt regrade <name>`
re-grades a finished run with the current weights.

`OUTPUT_DIR` is `"$HOME/Documents/Claude/Cowork Projects/YoutubeSummaries"` by default. The name has
a space, so keep it in double quotes in a shell command.
