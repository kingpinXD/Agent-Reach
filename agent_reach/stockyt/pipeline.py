# -*- coding: utf-8 -*-
"""Run the stages in order, each with its check, and turn the first stop into an exit code.

A stage is skipped when it finished `done`, its outputs exist and the hash of its inputs (the
files it reads, plus the parameters and settings it uses) is the one stored in run.json.
Exit codes: 0 done · 1 a check failed · 2 blocked on the user · 3 waiting on session LLM tasks.
"""

from __future__ import annotations

import glob
import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime

from agent_reach.stockyt import STOCKYT_VERSION, stages, upstream
from agent_reach.stockyt import cache as cache_mod
from agent_reach.stockyt import run as run_mod
from agent_reach.stockyt.config import Settings, Weights
from agent_reach.stockyt.llm.base import LLMBackend, LLMError, LLMTask, canonical_json
from agent_reach.stockyt.llm.claude_cli import ClaudeCLIBackend
from agent_reach.stockyt.llm.fake import FakeBackend
from agent_reach.stockyt.llm.session import SessionBackend
from agent_reach.stockyt.stages import Ctx

EXIT_CODES = {"done": 0, "failed": 1, "blocked": 2, "waiting": 3}
BACKENDS = ("claude-cli", "session", "fake")

# Answers for `--llm fake`, by task id. Tests fill it; from a terminal every task stays pending.
FAKE_ANSWERS: dict = {}

Inputs = Callable[[Ctx], dict]
Outputs = Callable[[Ctx], list[str]]


@dataclass(frozen=True)
class Stage:
    name: str
    run: Callable[[Ctx], tuple[str, list[str]]]
    inputs: Inputs
    outputs: Outputs


def _digest(paths: list[str]) -> dict:
    """sha256 per file, keyed by its name; a missing file reads as None."""
    out: dict[str, str | None] = {}
    for path in paths:
        try:
            with open(path, "rb") as f:
                out[os.path.basename(path)] = hashlib.sha256(f.read()).hexdigest()
        except FileNotFoundError:
            out[os.path.basename(path)] = None
    return out


def _chunk_files(ctx: Ctx, *patterns: str) -> list[str]:
    return sorted(p for pattern in patterns for p in glob.glob(os.path.join(ctx.chunk_dir, pattern)))


def _llm_inputs(ctx: Ctx, *patterns: str) -> dict:
    return {"files": _digest(_chunk_files(ctx, *patterns)), "model": ctx.settings.llm_model,
            "version": STOCKYT_VERSION}


def _transcripts_inputs(ctx: Ctx) -> dict:
    """The caption tracks count only when set, so a run started before they existed keeps its hash."""
    inputs = {"links": _digest([ctx.run.file("links.txt")]), "lang": ctx.settings.lang,
              "whisper": ctx.settings.whisper}
    if ctx.settings.caption_langs:
        inputs["caption_langs"] = ctx.settings.caption_langs
    return inputs


STAGES: list[Stage] = [
    Stage("channels", stages.run_channels,
          lambda ctx: {"since": ctx.run.data["params"]["since"], "until": ctx.run.data["params"].get("until"),
                       "channels": ctx.settings.channels, "timezone": ctx.settings.timezone},
          lambda ctx: [ctx.run.file("links.txt"), ctx.run.file("channels.json")]),
    Stage("transcripts", stages.run_transcripts, _transcripts_inputs,
          lambda ctx: [ctx.run.file("transcripts.jsonl")]),
    Stage("corpus", stages.run_corpus,
          lambda ctx: {"transcripts": _digest([ctx.run.file("transcripts.jsonl")]),
                       "chunk_chars": ctx.settings.chunk_chars},
          lambda ctx: [os.path.join(ctx.chunk_dir, "authors.json")]),
    Stage("extract", stages.run_extract,
          lambda ctx: _llm_inputs(ctx, "chunk_*.jsonl"),
          lambda ctx: [p.replace("chunk_", "extract_", 1) for p in _chunk_files(ctx, "chunk_*.jsonl")]),
    Stage("review", stages.run_review,
          lambda ctx: _llm_inputs(ctx, "chunk_*.jsonl", "extract_*.jsonl"),
          lambda ctx: [os.path.join(ctx.chunk_dir, "review_summary.json")]),
    Stage("grade", stages.run_grade,
          lambda ctx: {"files": _digest(_chunk_files(ctx, "authors.json", "review_summary.json", "reviewed_*.jsonl",
                                                     "extract_*.videos.json")),
                       "weights": ctx.settings.snapshot()["weights"], "version": STOCKYT_VERSION},
          lambda ctx: [ctx.run.file("graded.json")]),
    Stage("report", stages.run_report,
          lambda ctx: {"files": _digest([ctx.run.file("graded.json"),
                                         *_chunk_files(ctx, "review_*.json", "response_*.json", "reviewed_*.jsonl")]),
                       "version": STOCKYT_VERSION},
          lambda ctx: [ctx.run.file("tickers.json"), ctx.run.file("report.md")]),
]


def input_hash(inputs: dict) -> str:
    return hashlib.sha256(canonical_json(inputs).encode("utf-8")).hexdigest()


def _unchanged(ctx: Ctx, stage: Stage, digest: str) -> bool:
    record = ctx.run.data["stages"].get(stage.name) or {}
    return (record.get("status") == "done" and record.get("input_hash") == digest
            and all(os.path.exists(p) for p in stage.outputs(ctx)))


def run_stages(ctx: Ctx, stage_list: list[Stage] = STAGES, force: bool = False,
               echo: Callable[[str], None] = print) -> str:
    """Run the stages in order until one does not finish `done`; return the last status."""
    for stage in stage_list:
        digest = input_hash(stage.inputs(ctx))
        if not force and _unchanged(ctx, stage, digest):
            echo(f"[{stage.name}] unchanged, skipped")
            continue
        run_mod.start_stage(ctx.run, stage.name)
        status, lines = stage.run(ctx)
        run_mod.finish_stage(ctx.run, stage.name, status, lines, digest)
        echo(f"[{stage.name}]")
        for line in lines:
            echo(f"  {line}")
        if status != "done":
            return status
    return "done"


# ── backends ──

class LoggedBackend:
    """Wraps a backend so every LLM call gets a log.jsonl line."""

    def __init__(self, backend: LLMBackend, log: run_mod.LogCall):
        self.backend = backend
        self.log = log

    def run(self, task: LLMTask) -> dict | None:
        started = time.monotonic()
        try:
            answer = self.backend.run(task)
        except Exception:
            self.log("llm", task.id, time.monotonic() - started, "error")
            raise
        self.log("llm", task.id, time.monotonic() - started, "pending" if answer is None else "answered")
        return answer


def session_dir(run: run_mod.Run) -> str:
    return run.file("llm", "session")


def make_backend(name: str, settings: Settings, run: run_mod.Run) -> LLMBackend:
    if name == "claude-cli":
        return ClaudeCLIBackend(model=settings.llm_model)
    if name == "session":
        return SessionBackend(session_dir(run))
    if name == "fake":
        return FakeBackend(FAKE_ANSWERS)
    raise ValueError(f"unknown LLM backend {name!r}; use one of {', '.join(BACKENDS)}")


def pending_prompts(run: run_mod.Run) -> list[str]:
    """Session prompt files that have no answer file yet."""
    prompts = sorted(glob.glob(os.path.join(session_dir(run), "*.prompt.md")))
    return [p for p in prompts if not os.path.exists(p[: -len(".prompt.md")] + ".answer.json")]


# ── doctor ──

Check = tuple[bool | None, str]  # (ok, line); ok None is information only


def _check_ytdlp() -> Check:
    try:
        version = upstream.run_ytdlp(["--version"], 60).strip()
    except Exception as e:
        return False, f"yt-dlp: {e}"
    return True, f"yt-dlp {version}"


def _check_js_runtime() -> Check:
    found = next((name for name in ("deno", "node") if shutil.which(name)), None)
    return (True, f"JS runtime: {found}") if found else (False, "JS runtime: neither deno nor node is on PATH")


def _check_output_dir(output_dir: str) -> Check:
    try:
        os.makedirs(output_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output_dir):
            pass
    except OSError as e:
        return False, f"output dir {output_dir}: not writable ({e})"
    return True, f"output dir {output_dir}: writable"


_PING = LLMTask(
    id="doctor",
    prompt='Reply with {"ok": true}.',
    schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
    system="Answer with JSON only.",
)


def _check_claude(settings: Settings) -> Check:
    try:
        answer = ClaudeCLIBackend(model=settings.llm_model, timeout=120).run(_PING)
    except LLMError as e:
        return False, f"claude -p: {e}"
    return (answer.get("ok") is True), f"claude -p: answered {answer}"


def _cache_freshness(settings: Settings, today: date) -> list[Check]:
    lines: list[Check] = []
    for name, channel_id in settings.channels:
        rows = cache_mod.load(channel_id, settings.cache_dir)
        if not rows:
            lines.append((None, f"cache {name}: none yet"))
            continue
        newest = max(r["upload_date"] for r in rows.values())
        age = (today - datetime.strptime(newest, "%Y%m%d").date()).days
        lines.append((None, f"cache {name}: newest video {newest} ({age} days old)"))
    return lines


def doctor(settings: Settings, backend: str) -> list[Check]:
    checks = [_check_ytdlp(), _check_js_runtime(), _check_output_dir(settings.output_dir)]
    if backend == "claude-cli":
        checks.append(_check_claude(settings))
    return checks + _cache_freshness(settings, date.today())


def doctor_lines(checks: list[Check]) -> list[str]:
    tag = {True: "OK", False: "FAIL", None: "INFO"}
    return [f"[{tag[ok]:4}] {line}" for ok, line in checks]


# ── entry points ──

def settings_for(run: run_mod.Run) -> Settings:
    """The run's settings: its config snapshot, with the run's LLM backend and cookies."""
    params = run.data["params"]
    return replace(Settings.from_snapshot(run.data["config_snapshot"]),
                   llm_backend=params["llm"], cookies_from_browser=params.get("cookies_from_browser"))


def _next_action(status: str, ctx: Ctx) -> list[str]:
    run = ctx.run
    label = run.label
    if status == "blocked":
        return ["Blocked: YouTube is rate limiting. Ask the user before sending browser cookies, then run:",
                f"  agent-reach stockyt run --resume {label} --cookies-from-browser chrome"]
    if status == "waiting":
        prompts = pending_prompts(run)
        return ["Waiting on LLM tasks. Answer each prompt file below (it names its answer file), then run:",
                f"  agent-reach stockyt run --resume {label}", *(f"  {p}" for p in prompts)]
    if status == "failed":
        lines = [f"Failed: the [{name}] check above did not pass." for name, rec in run.data["stages"].items()
                 if rec["status"] == "failed"]
        if ctx.failed_tasks():
            lines.append(f"To ask the unrecovered LLM tasks again: agent-reach stockyt run --resume {label} "
                         "--retry-failed")
        return lines
    return [f"Done: {run.file('report.md')}"]


def execute(run: run_mod.Run, *, params: dict | None = None, weights: Weights | None = None,
            retry_failed: bool = False, stage_list: list[Stage] = STAGES, force: bool = False,
            echo: Callable[[str], None] = print) -> int:
    """Hold the run's lock, apply the changes asked for, run the stages and print the one action needed.

    `params` updates run.json's params (the LLM backend, cookies); `weights` replaces the snapshot's
    grading weights (regrade); `retry_failed` forgets the unrecovered LLM tasks. Returns the exit code.
    """
    with run_mod.locked(run):
        run.data = run_mod.read_json(run.file("run.json"))  # another process may have saved it
        run.data["params"].update(params or {})
        if weights is not None:
            run.data["config_snapshot"]["weights"] = asdict(weights)
        settings = settings_for(run)
        ctx = Ctx(run, settings, LoggedBackend(make_backend(settings.llm_backend, settings, run),
                                               lambda *a: run_mod.log(run, *a)))
        if retry_failed:
            ctx.failed_tasks().clear()
        run_mod.save(run)
        try:
            status = run_stages(ctx, stage_list, force=force, echo=echo)
        finally:
            run_mod.save(run)
        for line in _next_action(status, ctx):
            echo(line)
    return EXIT_CODES[status]
