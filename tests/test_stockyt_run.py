# -*- coding: utf-8 -*-
"""Tests for the run folder: run.json, stage records, log.jsonl, the lock and atomic writes."""

import json
import os

import pytest

from agent_reach.stockyt import STOCKYT_VERSION
from agent_reach.stockyt import run as run_mod


def _run(tmp_path):
    return run_mod.create(str(tmp_path), "postcrash", {"since": "20261106"}, {"lang": "en"})


def test_create_writes_schema_1_run_json(tmp_path):
    run = _run(tmp_path)
    assert run.path == str(tmp_path / "runs" / "postcrash")
    on_disk = json.loads((tmp_path / "runs" / "postcrash" / "run.json").read_text(encoding="utf-8"))
    assert on_disk == {
        "schema": 1,
        "stockyt_version": STOCKYT_VERSION,
        "params": {"since": "20261106"},
        "config_snapshot": {"lang": "en"},
        "stages": {},
    }


def test_create_refuses_an_existing_run_and_open_reads_it_back(tmp_path):
    _run(tmp_path)
    with pytest.raises(FileExistsError, match="resume it instead"):
        _run(tmp_path)
    assert run_mod.open_run(str(tmp_path), "postcrash").data["params"] == {"since": "20261106"}


def test_open_missing_run_names_it(tmp_path):
    with pytest.raises(FileNotFoundError, match="no run 'nope'"):
        run_mod.open_run(str(tmp_path), "nope")


@pytest.mark.parametrize("label", ["../escape", "a/b", "", ".hidden"])
def test_label_cannot_leave_the_runs_folder(tmp_path, label):
    with pytest.raises(ValueError, match="run label"):
        run_mod.run_dir(str(tmp_path), label)


def test_stage_lifecycle_is_saved(tmp_path):
    run = _run(tmp_path)
    assert run_mod.stage(run, "channels")["status"] == "pending"
    run_mod.start_stage(run, "channels")
    run_mod.start_stage(run, "channels")
    run_mod.finish_stage(run, "channels", "failed", ["Tom Nash: listing ended before the window start"], "abc")

    saved = run_mod.open_run(str(tmp_path), "postcrash").data["stages"]["channels"]
    assert saved["status"] == "failed"
    assert saved["attempts"] == 2
    assert saved["input_hash"] == "abc"
    assert saved["gate_lines"] == ["Tom Nash: listing ended before the window start"]
    assert saved["finished_at"].endswith("Z")


def test_finish_stage_rejects_an_unknown_status(tmp_path):
    with pytest.raises(ValueError, match="unknown stage status 'ok'"):
        run_mod.finish_stage(_run(tmp_path), "channels", "ok", [])


def test_log_appends_one_line_per_call(tmp_path):
    run = _run(tmp_path)
    run_mod.log(run, "captions", "vid1", 1.23456, "ok")
    run_mod.log(run, "captions", "vid2", 0.5, "rate_limited")
    lines = [json.loads(x) for x in (tmp_path / "runs/postcrash/log.jsonl").read_text().splitlines()]
    assert [(x["kind"], x["id"], x["duration"], x["outcome"]) for x in lines] == [
        ("captions", "vid1", 1.235, "ok"),
        ("captions", "vid2", 0.5, "rate_limited"),
    ]


def test_lock_is_exclusive_and_released(tmp_path):
    run = _run(tmp_path)
    with run_mod.locked(run):
        assert os.path.exists(run.file(".lock"))
        with pytest.raises(run_mod.RunLocked, match=f"locked by pid {os.getpid()}"):
            with run_mod.locked(run):
                pass
    assert not os.path.exists(run.file(".lock"))
    with run_mod.locked(run):  # free again
        pass


def test_atomic_write_leaves_the_old_file_when_the_write_fails(tmp_path):
    target = tmp_path / "out" / "x.json"
    run_mod.atomic_write_json(str(target), {"a": 1})
    with pytest.raises(TypeError):
        run_mod.atomic_write_json(str(target), {"a": object()})
    assert json.loads(target.read_text()) == {"a": 1}
    assert [p.name for p in target.parent.iterdir()] == ["x.json"]  # no temp file left behind
