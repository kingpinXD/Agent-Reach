# -*- coding: utf-8 -*-
"""Valid LLM answers on disk, one `<dir>/<key>.json` per answer, so a resume never repeats a call."""

from __future__ import annotations

import os

from agent_reach.stockyt.run import atomic_write_json, read_json


def _path(cache_dir: str, key: str) -> str:
    return os.path.join(cache_dir, f"{key}.json")


def load(cache_dir: str, key: str) -> dict | None:
    path = _path(cache_dir, key)
    return read_json(path) if os.path.exists(path) else None


def save(cache_dir: str, key: str, answer: dict) -> None:
    atomic_write_json(_path(cache_dir, key), answer)
