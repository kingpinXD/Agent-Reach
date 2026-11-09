# -*- coding: utf-8 -*-
"""A run folder: `OUTPUT_DIR/runs/<label>/` with run.json, log.jsonl and a .lock.

run.json (schema 1) records the stockyt version, the run's parameters, a config snapshot and,
per stage, {status, attempts, input_hash, gate_lines, finished_at}. Every JSON write goes to a
temp file first and is then renamed into place, so a crash never leaves half a file.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from agent_reach.stockyt import STOCKYT_VERSION

SCHEMA = 1
STATUSES = ("pending", "done", "failed", "blocked", "waiting")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class RunLocked(RuntimeError):
    """Another process holds the run's .lock."""


@dataclass
class Run:
    path: str
    data: dict

    @property
    def label(self) -> str:
        return os.path.basename(self.path)

    def file(self, *parts: str) -> str:
        return os.path.join(self.path, *parts)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_text(path: str, text: str) -> None:
    """Write `text` to a temp file beside `path`, then rename it over `path`."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def atomic_write_json(path: str, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def run_dir(output_dir: str, label: str) -> str:
    if not _LABEL_RE.match(label):
        raise ValueError(f"run label {label!r} may use only letters, digits, '.', '_' and '-'")
    return os.path.join(output_dir, "runs", label)


def create(output_dir: str, label: str, params: dict, config_snapshot: dict) -> Run:
    """Start a new run folder; refuses one that already has a run.json."""
    path = run_dir(output_dir, label)
    if os.path.exists(os.path.join(path, "run.json")):
        raise FileExistsError(f"run {label!r} already exists at {path}; resume it instead")
    run = Run(path, {
        "schema": SCHEMA,
        "stockyt_version": STOCKYT_VERSION,
        "params": params,
        "config_snapshot": config_snapshot,
        "stages": {},
    })
    save(run)
    return run


def open_run(output_dir: str, label: str) -> Run:
    path = run_dir(output_dir, label)
    try:
        with open(os.path.join(path, "run.json"), encoding="utf-8") as f:
            return Run(path, json.load(f))
    except FileNotFoundError:
        raise FileNotFoundError(f"no run {label!r} at {path}") from None


def save(run: Run) -> None:
    atomic_write_json(run.file("run.json"), run.data)


def stage(run: Run, name: str) -> dict:
    """The stage's record, created as pending on first use."""
    return run.data["stages"].setdefault(name, {
        "status": "pending",
        "attempts": 0,
        "input_hash": None,
        "gate_lines": [],
        "finished_at": None,
    })


def start_stage(run: Run, name: str) -> None:
    stage(run, name)["attempts"] += 1
    save(run)


def finish_stage(run: Run, name: str, status: str, gate_lines: list[str], input_hash: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown stage status {status!r}")
    stage(run, name).update(status=status, gate_lines=list(gate_lines), input_hash=input_hash, finished_at=_now())
    save(run)


def log(run: Run, kind: str, item_id: str, duration: float, outcome: str) -> None:
    """Append one line per outside call to log.jsonl."""
    line = {"at": _now(), "kind": kind, "id": item_id, "duration": round(duration, 3), "outcome": outcome}
    with open(run.file("log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


@contextmanager
def locked(run: Run) -> Iterator[None]:
    """Hold the run's .lock for the duration; a second holder gets RunLocked."""
    lock_path = run.file(".lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        with open(lock_path, encoding="utf-8", errors="replace") as f:
            holder = f.read().strip() or "unknown"
        raise RunLocked(
            f"run {run.label!r} is locked by pid {holder} ({lock_path}); "
            "delete that file only if no stockyt run is still active"
        ) from None
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    try:
        yield
    finally:
        os.unlink(lock_path)
