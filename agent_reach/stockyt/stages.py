# -*- coding: utf-8 -*-
"""The pipeline's stages: channels → transcripts → corpus → extract → review → grade → report.

Each stage is a plain function `(ctx) -> (status, gate lines)`; status is done | failed | blocked
| waiting. A stage reads the files the stages before it wrote and writes its own; the pipeline
(`pipeline.py`) decides which stages run.

Run folder layout (`OUTPUT_DIR/runs/<label>/`):
  links.txt, channels.json              channels
  transcripts.jsonl                     transcripts
  chunks/authors.json, chunk_NN.jsonl   corpus
  chunks/extract_NN.jsonl, extract_NN.videos.json                         extract
  chunks/review_NN_r{1,2}.json, response_NN_r{1,2}.json, reviewed_NN.jsonl,
  chunks/review_summary.json                                               review
  graded.json                           grade
  tickers.json, report.md               report
  llm/cache/, llm/session/              LLM answers, and the session backend's prompt files
"""

from __future__ import annotations

import functools
import glob
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from agent_reach.stockyt import channels as channels_mod
from agent_reach.stockyt import report as report_mod
from agent_reach.stockyt import review as review_mod
from agent_reach.stockyt import run as run_mod
from agent_reach.stockyt import transcripts as transcripts_mod
from agent_reach.stockyt.config import Settings
from agent_reach.stockyt.corpus import split_corpus
from agent_reach.stockyt.grading import grade
from agent_reach.stockyt.llm.base import LLMBackend, LLMTask, cache_key
from agent_reach.stockyt.llm.runner import Validator, ask_many
from agent_reach.stockyt.run import (
    Run,
    atomic_write_json,
    atomic_write_jsonl,
    atomic_write_text,
    read_json,
    read_jsonl,
)
from agent_reach.stockyt.tasks import extract as extract_task
from agent_reach.stockyt.tasks import review as review_task

Job = tuple[LLMTask, Validator]
Answer = tuple["dict | None", list[str]]

EXTRACTED = ("done", "no_tickers")
_CHUNK_RE = re.compile(r"chunk_(\d+)\.jsonl")
# Accepted findings of these kinds change or add a row, which the reviewer re-checks in round 2.
_ROW_CHANGING = ("wrong_ticker", "wrong_sentiment", "missed_ticker")


@dataclass
class Ctx:
    run: Run
    settings: Settings
    backend: LLMBackend
    sleep: Callable[[float], None] = time.sleep

    @property
    def chunk_dir(self) -> str:
        return self.run.file("chunks")

    @property
    def llm_cache_dir(self) -> str:
        return self.run.file("llm", "cache")

    @property
    def log(self):
        return functools.partial(run_mod.log, self.run)

    def failed_tasks(self) -> dict:
        """{cache key: {task, errors}} of LLM tasks still invalid after the one re-ask (unrecovered)."""
        return self.run.data.setdefault("failed_tasks", {})


def chunks(chunk_dir: str) -> dict[str, list[dict]]:
    """{NN: the chunk's transcript records}, in chunk order."""
    paths = sorted(glob.glob(os.path.join(chunk_dir, "chunk_*.jsonl")))
    matches = ((_CHUNK_RE.fullmatch(os.path.basename(p)), p) for p in paths)
    return {m.group(1): read_jsonl(p) for m, p in matches if m}


def ask_jobs(ctx: Ctx, jobs: list[Job]) -> dict[str, Answer]:
    """Ask every job, keyed by task id. A task already unrecovered is not asked again; its errors stand."""
    failed = ctx.failed_tasks()
    model = ctx.settings.llm_model
    keys = {task.id: cache_key(task, model) for task, _ in jobs}
    todo = [(task, validate) for task, validate in jobs if keys[task.id] not in failed]
    answers = ask_many(ctx.backend, todo, ctx.llm_cache_dir, model, ctx.settings.llm_workers) if todo else {}
    for task_id, (_, errors) in answers.items():
        if errors:
            failed[keys[task_id]] = {"task": task_id, "errors": errors}
    return {task.id: (None, failed[keys[task.id]]["errors"]) if keys[task.id] in failed else answers[task.id]
            for task, _ in jobs}


def _failed_lines(failures: dict[str, list[str]]) -> list[str]:
    lines = []
    for task_id, errors in failures.items():
        shown = "; ".join(errors[:3]) + (f"; … {len(errors) - 3} more" if len(errors) > 3 else "")
        lines.append(f"❌ {task_id}: unrecovered after one re-ask — {shown}")
    return lines


# ── channels ──

def run_channels(ctx: Ctx) -> tuple[str, list[str]]:
    params, settings = ctx.run.data["params"], ctx.settings
    cutoff = channels_mod.parse_since(params["since"], settings.timezone)
    results = channels_mod.sweep(settings.channels, cutoff, until=params.get("until"), cache_dir=settings.cache_dir,
                                 pace_seconds=settings.pace_seconds, sleep=ctx.sleep, log=ctx.log)
    atomic_write_text(ctx.run.file("links.txt"), "".join(url + "\n" for url in channels_mod.window_urls(results)))
    atomic_write_json(ctx.run.file("channels.json"), [
        {"channel": r.name, "channel_id": r.channel_id, "count": len(r.rows), "reached_cutoff": r.reached_cutoff,
         "error": r.error, "videos": r.rows}
        for r in results
    ])
    return channels_mod.check(results)


# ── transcripts ──

def run_transcripts(ctx: Ctx) -> tuple[str, list[str]]:
    links = transcripts_mod.read_link_file(ctx.run.file("links.txt"))
    rows = transcripts_mod.fetch_all(links, ctx.run.file("transcripts.jsonl"), ctx.settings,
                                     sleep=ctx.sleep, log=ctx.log)
    return transcripts_mod.check(links, rows)


# ── corpus ──

_DERIVED = ("extract_*", "review_*", "response_*", "reviewed_*")


def run_corpus(ctx: Ctx) -> tuple[str, list[str]]:
    """Split the transcripts into chunks. Every file derived from old chunks goes, session answers included."""
    os.makedirs(ctx.chunk_dir, exist_ok=True)
    stale = [os.path.join(ctx.chunk_dir, pattern) for pattern in _DERIVED] + [ctx.run.file("llm", "session", "*")]
    for pattern in stale:
        for path in glob.glob(pattern):
            os.remove(path)
    rows = transcripts_mod.read_rows(ctx.run.file("transcripts.jsonl"))
    authors, n_chunks = split_corpus(rows, ctx.chunk_dir, ctx.settings.chunk_chars)
    ok = authors["N"] > 0
    line = (f"Corpus: {authors['N']} videos · {authors['total_authors']} channels · {n_chunks} chunks "
            f"[{'OK' if ok else 'FAIL'}]")
    return ("done", [line]) if ok else ("failed", [line, "No video has a transcript to read"])


# ── extract ──

def extract_counts(chunk_dir: str) -> dict:
    """{n, extracted, missing: [NN]} from the files: chunk videos marked done or no_tickers in extract_NN.videos.json."""
    n = extracted = 0
    missing = []
    for nn, records in chunks(chunk_dir).items():
        path = os.path.join(chunk_dir, f"extract_{nn}.videos.json")
        statuses = read_json(path) if os.path.exists(path) else {}
        done = sum(statuses.get(r["video_id"]) in EXTRACTED for r in records)
        n += len(records)
        extracted += done
        if done < len(records):
            missing.append(nn)
    return {"n": n, "extracted": extracted, "missing": missing}


def extract_line(counts: dict) -> str:
    ok = counts["n"] > 0 and counts["extracted"] == counts["n"]
    return f"Extract: {counts['extracted']}/{counts['n']} videos [{'OK' if ok else 'FAIL'}]"


def write_extract(chunk_dir: str, nn: str, records: list[dict], answer: dict | None) -> None:
    """extract_NN.jsonl and extract_NN.videos.json from a valid answer; every video `skipped` without one."""
    videos_path = os.path.join(chunk_dir, f"extract_{nn}.videos.json")
    if answer is None:
        atomic_write_json(videos_path, {r["video_id"]: "skipped" for r in records})
        return
    atomic_write_jsonl(os.path.join(chunk_dir, f"extract_{nn}.jsonl"), extract_task.to_rows(answer))
    atomic_write_json(videos_path, {v["video_id"]: v["status"] for v in answer["videos"]})


def run_extract(ctx: Ctx) -> tuple[str, list[str]]:
    by_chunk = chunks(ctx.chunk_dir)
    answers = ask_jobs(ctx, [extract_task.job(f"extract_{nn}", records, ctx.settings.market)
                             for nn, records in by_chunk.items()])
    pending, failures = [], {}
    for nn, records in by_chunk.items():
        answer, errors = answers[f"extract_{nn}"]
        if errors:
            failures[f"extract_{nn}"] = errors
            write_extract(ctx.chunk_dir, nn, records, None)
        elif answer is None:
            pending.append(nn)
        else:
            write_extract(ctx.chunk_dir, nn, records, answer)

    counts = extract_counts(ctx.chunk_dir)
    lines = [extract_line(counts), *_failed_lines(failures)]
    if pending:
        return "waiting", lines + [f"⏳ {len(pending)} chunk(s) waiting for their extraction answer"]
    if failures or counts["missing"] or not counts["n"]:
        return "failed", lines
    return "done", lines


# ── review ──

def needs_r2(r1: list[dict], resp1: list[dict]) -> bool:
    """Round 2 only when an R1 finding was rejected, or an accepted one changed or added a row."""
    decisions = {r.get("id"): r.get("decision") for r in resp1}
    return any(decisions.get(f["id"]) == "reject"
               or (decisions.get(f["id"]) == "accept" and f["issue"] in _ROW_CHANGING) for f in r1)


def review_chunk(nn: str, records: list[dict], rows: list[dict], ask: Callable[[Job], Answer],
                 out_dir: str, market: str = "us") -> tuple[str, list[str]]:
    """At most two review rounds for one chunk, as stateless calls; writes the four review files.

    A call with nothing to answer is skipped and code writes its empty result. Returns
    ("done", []), ("waiting", []) when an answer is pending, or ("failed", errors).
    """
    def write(name: str, value) -> None:
        atomic_write_json(os.path.join(out_dir, name), value)

    def step(job: Job) -> tuple[Any, tuple[str, list[str]] | None]:
        answer, errors = ask(job)
        if errors:
            return None, ("failed", errors)
        if answer is None:
            return None, ("waiting", [])
        return review_task.to_file(answer), None

    r1, stop = step(review_task.r1_review_job(f"review_{nn}_r1", records, rows, market))
    if stop:
        return stop
    write(f"review_{nn}_r1.json", r1)

    resp1: list[dict] = []
    if r1:
        resp1, stop = step(review_task.response_job(f"response_{nn}_r1", records, rows, r1, last_round=False,
                                                    market=market))
        if stop:
            return stop
    write(f"response_{nn}_r1.json", resp1)

    r2 = review_task.EMPTY_R2
    if needs_r2(r1, resp1):
        r2, stop = step(review_task.r2_review_job(f"review_{nn}_r2", records, rows, r1, resp1, market))
        if stop:
            return stop
    write(f"review_{nn}_r2.json", r2)

    resp2: list[dict] = []
    if r2["findings"]:
        rows_after = review_task.rows_after_r1(rows, r1, resp1)
        resp2, stop = step(review_task.response_job(f"response_{nn}_r2", records, rows_after, r2["findings"],
                                                    last_round=True, market=market))
        if stop:
            return stop
    write(f"response_{nn}_r2.json", resp2)
    return "done", []


def review_lines(counts: dict) -> list[str]:
    """review_run's counts as printed lines, ending in `Review: OK` or `Review: FAIL`."""
    lines = [f"Reviewed {counts['reviewed_chunks']}/{counts['chunks']} chunks: "
             f"{counts['confirmed']} confirmed · {counts['corrected']} corrected · {counts['added']} added · "
             f"{counts['dropped']} dropped · {counts['disputed']} disputed "
             f"(+{counts['disputed_missed']} disputed missed tickers)"]
    lines += [f"  ❌ {problem}" for problem in counts["problems"]]
    return lines + ["Review: OK" if counts["ok"] else "Review: FAIL"]


def run_review(ctx: Ctx) -> tuple[str, list[str]]:
    by_chunk = chunks(ctx.chunk_dir)

    def ask(job: Job) -> Answer:
        return ask_jobs(ctx, [job])[job[0].id]

    def one(item) -> tuple[str, list[str]]:
        nn, records = item
        rows = read_jsonl(os.path.join(ctx.chunk_dir, f"extract_{nn}.jsonl"))
        return review_chunk(nn, records, rows, ask, ctx.chunk_dir, ctx.settings.market)

    with ThreadPoolExecutor(max_workers=ctx.settings.llm_workers) as pool:
        outcomes = dict(zip(by_chunk, pool.map(one, by_chunk.items())))

    waiting = [nn for nn, (st, _) in outcomes.items() if st == "waiting"]
    failures = {f"review chunk {nn}": errors for nn, (st, errors) in outcomes.items() if st == "failed"}
    if waiting:
        return "waiting", [f"⏳ {len(waiting)} chunk(s) waiting for a review answer", *_failed_lines(failures)]
    if failures:
        return "failed", ["Review: FAIL", *_failed_lines(failures)]
    counts = review_mod.review_run(ctx.chunk_dir)
    return ("done" if counts["ok"] else "failed"), review_lines(counts)


# ── grade ──

def graded_result(chunk_dir: str, settings: Settings) -> dict:
    """The graded leaderboards over the reviewed rows, with the review counts and the extract check."""
    rows, summary = review_mod.reviewed_rows(chunk_dir)
    authors = read_json(os.path.join(chunk_dir, "authors.json"))
    result = grade(rows, authors["videos_per_author"], authors["total_authors"], settings.weights)
    result["review"] = summary
    result["integrity"] = extract_counts(chunk_dir)
    return result


def run_grade(ctx: Ctx) -> tuple[str, list[str]]:
    try:
        result = graded_result(ctx.chunk_dir, ctx.settings)
    except ValueError as e:
        return "failed", [f"Grade: FAIL — {e}"]
    atomic_write_json(ctx.run.file("graded.json"), result)
    return "done", [f"Grade: {len(result['tickers'])} tickers · {len(result['bullish'])} bullish · "
                    f"{len(result['bearish'])} bearish [OK]"]


# ── report ──

def run_report(ctx: Ctx) -> tuple[str, list[str]]:
    result = read_json(ctx.run.file("graded.json"))
    changes = report_mod.review_changes(ctx.chunk_dir)
    _, md_path = report_mod.write(ctx.run.path, result, changes)
    return "done", [*report_mod.console_lines(result), "",
                    f"Report: {len(changes)} review changes → {md_path} [OK]"]
