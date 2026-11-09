# -*- coding: utf-8 -*-
"""Tests for the stage functions (stages.py) and the stage runner, backends and doctor (pipeline.py)."""

import json
import os
from pathlib import Path

import pytest

from agent_reach.stockyt import pipeline, stages
from agent_reach.stockyt import run as run_mod
from agent_reach.stockyt.config import Settings
from agent_reach.stockyt.llm.base import LLMError, LLMTask
from agent_reach.stockyt.llm.fake import FakeBackend
from agent_reach.stockyt.stages import Ctx

TRANSCRIPT = "Nvidia is my top pick. I am loading up on NVDA. AMD is a stock I hold."
RECORDS = [{"video_id": "v1", "channel": "C", "transcript": TRANSCRIPT}]


def _finding(fid, issue="not_investable", ticker="AMD", proposed=None):
    return {"id": fid, "video_id": "v1", "ticker": ticker, "issue": issue, "proposed": proposed or {},
            "quote": "AMD is a stock I hold"}


def _response(fid, decision):
    return {"id": fid, "decision": decision, "quote": "AMD is a stock I hold", "reason": "r"}


# ── review skip rules ──

@pytest.mark.parametrize("r1, resp1, expected", [
    ([], [], False),
    ([_finding("f1")], [_response("f1", "accept")], False),  # an accepted drop changes no row R2 could check
    ([_finding("f1")], [_response("f1", "reject")], True),
    ([_finding("f1", "wrong_sentiment", proposed={"sentiment": "bearish"})], [_response("f1", "accept")], True),
    ([_finding("f1", "wrong_ticker", proposed={"ticker": "AMDX"})], [_response("f1", "accept")], True),
    ([_finding("f1", "missed_ticker", ticker=None)], [_response("f1", "accept")], True),
])
def test_needs_r2(r1, resp1, expected):
    assert stages.needs_r2(r1, resp1) is expected


def _asker(answers):
    """An `ask` for review_chunk: answers[task id] is an answer dict, None (pending) or a list of errors."""
    asked = []

    def ask(job):
        task, _ = job
        asked.append(task.id)
        answer = answers.get(task.id)
        return (None, answer) if isinstance(answer, list) else (answer, [])

    return ask, asked


def _files(tmp_path, nn="01"):
    names = [f"review_{nn}_r1.json", f"response_{nn}_r1.json", f"review_{nn}_r2.json", f"response_{nn}_r2.json"]
    return {n: json.loads((tmp_path / n).read_text(encoding="utf-8")) for n in names if (tmp_path / n).exists()}


def test_review_with_no_findings_asks_once_and_writes_the_empty_files(tmp_path):
    ask, asked = _asker({"review_01_r1": {"findings": []}})
    assert stages.review_chunk("01", RECORDS, [], ask, str(tmp_path)) == ("done", [])
    assert asked == ["review_01_r1"]
    assert _files(tmp_path) == {"review_01_r1.json": [], "response_01_r1.json": [],
                                "review_01_r2.json": {"verdicts": [], "findings": []}, "response_01_r2.json": []}


def test_an_accepted_drop_skips_round_2(tmp_path):
    ask, asked = _asker({"review_01_r1": {"findings": [_finding("f1")]},
                         "response_01_r1": {"responses": [_response("f1", "accept")]}})
    assert stages.review_chunk("01", RECORDS, [], ask, str(tmp_path)) == ("done", [])
    assert asked == ["review_01_r1", "response_01_r1"]


def test_r2_findings_get_a_last_round_response(tmp_path):
    r2 = {"verdicts": [{"id": "f1", "verdict": "hold"}], "findings": [_finding("r2-1")]}
    ask, asked = _asker({"review_01_r1": {"findings": [_finding("f1")]},
                         "response_01_r1": {"responses": [_response("f1", "reject")]},
                         "review_01_r2": r2,
                         "response_01_r2": {"responses": [_response("r2-1", "accept")]}})
    assert stages.review_chunk("01", RECORDS, [], ask, str(tmp_path)) == ("done", [])
    assert asked == ["review_01_r1", "response_01_r1", "review_01_r2", "response_01_r2"]
    assert _files(tmp_path)["review_01_r2.json"] == r2


def test_a_pending_answer_stops_the_chunk_and_keeps_what_is_done(tmp_path):
    ask, _ = _asker({"review_01_r1": {"findings": [_finding("f1")]}})
    assert stages.review_chunk("01", RECORDS, [], ask, str(tmp_path)) == ("waiting", [])
    assert list(_files(tmp_path)) == ["review_01_r1.json"]


def test_a_failed_answer_returns_its_errors(tmp_path):
    ask, _ = _asker({"review_01_r1": ["quote is not in the transcript"]})
    assert stages.review_chunk("01", RECORDS, [], ask, str(tmp_path)) == ("failed", ["quote is not in the transcript"])


# ── extract check ──

def _chunk(chunk_dir, nn, video_ids, statuses=None):
    chunk_dir.mkdir(exist_ok=True)
    (chunk_dir / f"chunk_{nn}.jsonl").write_text("".join(json.dumps({"video_id": v}) + "\n" for v in video_ids),
                                                 encoding="utf-8")
    if statuses is not None:
        (chunk_dir / f"extract_{nn}.videos.json").write_text(json.dumps(statuses), encoding="utf-8")


def test_extract_counts_only_done_and_no_tickers(tmp_path):
    _chunk(tmp_path, "01", ["a", "b"], {"a": "done", "b": "no_tickers"})
    _chunk(tmp_path, "02", ["c", "d"], {"c": "done", "d": "skipped"})
    _chunk(tmp_path, "03", ["e"])  # no status file: the extractor never finished it
    counts = stages.extract_counts(str(tmp_path))
    assert counts == {"n": 5, "extracted": 3, "missing": ["02", "03"]}
    assert stages.extract_line(counts) == "Extract: 3/5 videos [FAIL]"
    assert stages.extract_line({"n": 2, "extracted": 2, "missing": []}) == "Extract: 2/2 videos [OK]"
    assert stages.extract_line({"n": 0, "extracted": 0, "missing": []}) == "Extract: 0/0 videos [FAIL]"


# ── a run context ──

@pytest.fixture
def ctx(tmp_path):
    settings = Settings(output_dir=str(tmp_path), pace_seconds=0, llm_workers=1)
    run = run_mod.create(str(tmp_path), "t", {"since": "20261105", "until": None, "llm": "fake",
                                              "cookies_from_browser": None}, settings.snapshot())
    return Ctx(run, settings, FakeBackend({}))


def _task(task_id="extract_01", prompt="p"):
    return LLMTask(task_id, prompt, {"type": "object"})


def test_ask_jobs_remembers_an_unrecovered_task_until_its_prompt_changes(ctx):
    bad = FakeBackend({"extract_01": {"x": 1}, "extract_01-reask": {"x": 1}})
    ctx.backend = bad
    job = (_task(), lambda answer: ["wrong"])
    assert stages.ask_jobs(ctx, [job]) == {"extract_01": (None, ["wrong"])}
    assert stages.ask_jobs(ctx, [job]) == {"extract_01": (None, ["wrong"])}
    assert [t.id for t in bad.calls] == ["extract_01", "extract_01-reask"]  # asked once, plus its re-ask

    changed = (_task(prompt="a new prompt"), lambda answer: [])
    assert stages.ask_jobs(ctx, [changed])["extract_01"] == ({"x": 1}, [])

    ctx.failed_tasks().clear()  # what --retry-failed does
    ctx.backend = FakeBackend({"extract_01": {"x": 2}})
    assert stages.ask_jobs(ctx, [(_task(), lambda answer: [])])["extract_01"] == ({"x": 2}, [])


def test_run_corpus_clears_files_derived_from_old_chunks(ctx):
    chunk_dir = ctx.run.file("chunks")
    session = ctx.run.file("llm", "session")
    for path in (f"{chunk_dir}/extract_02.jsonl", f"{chunk_dir}/review_02_r1.json", f"{chunk_dir}/reviewed_02.jsonl",
                 f"{session}/extract_01.answer.json"):
        run_mod.atomic_write_text(path, "x")
    rows = [{"video_id": "v1", "url": "u", "channel": "C", "status": "ok", "transcript": "words"}]
    run_mod.atomic_write_text(ctx.run.file("transcripts.jsonl"), json.dumps(rows[0]) + "\n")

    assert stages.run_corpus(ctx) == ("done", ["Corpus: 1 videos · 1 channels · 1 chunks [OK]"])
    assert sorted(os.listdir(chunk_dir)) == ["authors.json", "chunk_01.jsonl"]
    assert os.listdir(session) == []


def test_review_stage_fails_when_the_apply_logic_finds_a_problem(ctx, monkeypatch):
    _chunk(Path(ctx.chunk_dir), "01", ["v1"])
    run_mod.atomic_write_text(f"{ctx.chunk_dir}/extract_01.jsonl", "")
    monkeypatch.setattr(stages, "review_chunk", lambda *a: ("done", []))
    failing = {"chunks": 1, "reviewed_chunks": 0, "confirmed": 0, "corrected": 0, "added": 0, "dropped": 0,
               "disputed": 0, "disputed_missed": 0, "problems": ["chunk 01: finding f1: no row"], "ok": False}
    monkeypatch.setattr(stages.review_mod, "review_run", lambda chunk_dir: failing)
    status, lines = stages.run_review(ctx)
    assert status == "failed"
    assert lines[-2:] == ["  ❌ chunk 01: finding f1: no row", "Review: FAIL"]


def test_grade_stage_fails_without_a_finished_review(ctx):
    status, lines = stages.run_grade(ctx)
    assert status == "failed" and lines[0].startswith("Grade: FAIL — no review_summary.json")


# ── the stage runner ──

def _stage(name, calls, status="done", inputs=None, outputs=()):
    def run(c):
        calls.append(name)
        return status, [f"{name} ran"]

    return pipeline.Stage(name, run, lambda c: inputs or {"name": name}, lambda c: list(outputs))


def test_run_stages_skips_unchanged_stages_and_stops_at_the_first_stop(ctx):
    calls, printed = [], []
    flow = [_stage("a", calls), _stage("b", calls, status="blocked"), _stage("c", calls)]
    assert pipeline.run_stages(ctx, flow, echo=printed.append) == "blocked"
    assert calls == ["a", "b"]
    assert {n: r["status"] for n, r in ctx.run.data["stages"].items()} == {"a": "done", "b": "blocked"}

    calls.clear()
    flow[1] = _stage("b", calls)
    assert pipeline.run_stages(ctx, flow, echo=printed.append) == "done"
    assert calls == ["b", "c"]
    assert "[a] unchanged, skipped" in printed

    calls.clear()
    assert pipeline.run_stages(ctx, flow, force=True, echo=printed.append) == "done"
    assert calls == ["a", "b", "c"]


def test_a_stage_runs_again_when_its_inputs_change_or_an_output_is_gone(ctx, tmp_path):
    calls = []
    assert pipeline.run_stages(ctx, [_stage("a", calls, inputs={"x": 1})], echo=lambda s: None) == "done"
    pipeline.run_stages(ctx, [_stage("a", calls, inputs={"x": 2})], echo=lambda s: None)
    pipeline.run_stages(ctx, [_stage("a", calls, inputs={"x": 2}, outputs=[str(tmp_path / "gone")])],
                        echo=lambda s: None)
    assert calls == ["a", "a", "a"]


def test_settings_for_applies_the_runs_backend_and_cookies(ctx):
    ctx.run.data["params"].update(llm="session", cookies_from_browser="chrome")
    settings = pipeline.settings_for(ctx.run)
    assert (settings.llm_backend, settings.cookies_from_browser) == ("session", "chrome")
    assert settings.pace_seconds == 0


def test_pending_prompts_are_the_prompt_files_without_an_answer(ctx):
    session = pipeline.session_dir(ctx.run)
    for name in ("extract_01.prompt.md", "extract_01.answer.json", "extract_02.prompt.md"):
        run_mod.atomic_write_text(f"{session}/{name}", "x")
    assert pipeline.pending_prompts(ctx.run) == [f"{session}/extract_02.prompt.md"]


def test_make_backend_rejects_an_unknown_name(ctx):
    with pytest.raises(ValueError, match="unknown LLM backend"):
        pipeline.make_backend("gpt", ctx.settings, ctx.run)


def test_logged_backend_writes_one_line_per_call(ctx):
    backend = pipeline.LoggedBackend(FakeBackend({"a": {"ok": True}}), lambda *a: run_mod.log(ctx.run, *a))
    backend.run(_task("a"))
    backend.run(_task("b"))
    lines = [json.loads(line) for line in open(ctx.run.file("log.jsonl"), encoding="utf-8")]
    assert [(line["kind"], line["id"], line["outcome"]) for line in lines] == [
        ("llm", "a", "answered"), ("llm", "b", "pending")]


# ── doctor ──

def test_doctor_reports_each_check(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.upstream, "run_ytdlp", lambda args, timeout: "2026.08.19\n")
    monkeypatch.setattr(pipeline.shutil, "which", lambda name: "/bin/node" if name == "node" else None)
    monkeypatch.setattr(pipeline.ClaudeCLIBackend, "run", lambda self, task: {"ok": True})
    cache_dir = tmp_path / ".cache" / "channels"
    cache_dir.mkdir(parents=True)
    (cache_dir / "UCa.jsonl").write_text(json.dumps({"video_id": "x", "upload_date": "20261101"}) + "\n",
                                         encoding="utf-8")
    settings = Settings(output_dir=str(tmp_path), channels=[("A", "UCa"), ("B", "UCb")])

    checks = pipeline.doctor(settings, "claude-cli")
    lines = pipeline.doctor_lines(checks)
    assert lines[0] == "[OK  ] yt-dlp 2026.08.19"
    assert lines[1] == "[OK  ] JS runtime: node"
    assert lines[2] == f"[OK  ] output dir {tmp_path}: writable"
    assert lines[3].startswith("[OK  ] claude -p")
    assert lines[4].startswith("[INFO] cache A: newest video 20261101 (")
    assert lines[5] == "[INFO] cache B: none yet"
    assert len(pipeline.doctor(settings, "session")) == len(checks) - 1  # claude -p only for claude-cli


def test_doctor_fails_each_broken_check(tmp_path, monkeypatch):
    def broken_ytdlp(args, timeout):
        raise RuntimeError("No module named yt_dlp")

    def broken_claude(self, task):
        raise LLMError("doctor: claude -p exited 1: not logged in")

    monkeypatch.setattr(pipeline.upstream, "run_ytdlp", broken_ytdlp)
    monkeypatch.setattr(pipeline.shutil, "which", lambda name: None)
    monkeypatch.setattr(pipeline.ClaudeCLIBackend, "run", broken_claude)
    blocked = tmp_path / "file"
    blocked.write_text("not a folder", encoding="utf-8")
    settings = Settings(output_dir=str(blocked / "out"), channels=[])

    assert [ok for ok, _ in pipeline.doctor(settings, "claude-cli")] == [False, False, False, False]
