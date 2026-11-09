# -*- coding: utf-8 -*-
"""Valid LLM answers on disk, one `<dir>/<key>.json` per answer, so a resume never repeats a call."""

from __future__ import annotations

import json
import os

from agent_reach.stockyt.run import atomic_write_json


def _path(cache_dir: str, key: str) -> str:
    return os.path.join(cache_dir, f"{key}.json")


def load(cache_dir: str, key: str) -> dict | None:
    path = _path(cache_dir, key)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(cache_dir: str, key: str, answer: dict) -> None:
    atomic_write_json(_path(cache_dir, key), answer)
