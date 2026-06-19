# -*- coding: utf-8 -*-
"""Per-channel JSONL cache of seen videos.

One file per channel at `<cache_dir>/<channel_id>.jsonl`, each line a row
{video_id, upload_date, title, channel_id}. Merges are a lossless union — we
never drop a previously cached video.
"""

from __future__ import annotations

import json
import os

DEFAULT_CACHE_DIR = os.path.expanduser("~/Downloads/YoutubeSummaries/.cache/channels")


def _cache_file(channel_id: str, cache_dir: str) -> str:
    return os.path.join(cache_dir, f"{channel_id}.jsonl")


def load(channel_id: str, cache_dir: str) -> dict[str, dict]:
    """Cached rows keyed by video_id. Empty dict if the file doesn't exist."""
    path = _cache_file(channel_id, cache_dir)
    if not os.path.exists(path):
        return {}
    rows: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            rows[row["video_id"]] = row
    return rows


def merge(channel_id: str, new_rows: list[dict], cache_dir: str) -> tuple[int, int]:
    """Union new_rows into the cache by video_id; return (added, total).

    Existing rows win on conflict so we never lose data already on disk. The
    file is rewritten sorted by upload_date descending (newest first).
    """
    os.makedirs(cache_dir, exist_ok=True)
    merged = load(channel_id, cache_dir)
    added = 0
    for row in new_rows:
        if row["video_id"] not in merged:
            merged[row["video_id"]] = row
            added += 1

    ordered = sorted(merged.values(), key=lambda r: r["upload_date"], reverse=True)
    path = _cache_file(channel_id, cache_dir)
    with open(path, "w", encoding="utf-8") as f:
        for row in ordered:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return added, len(ordered)


def all_in_window(channel_id: str, cutoff: str, cache_dir: str) -> list[dict]:
    """Cached rows with upload_date >= cutoff (YYYYMMDD), newest first."""
    rows = [r for r in load(channel_id, cache_dir).values() if r["upload_date"] >= cutoff]
    rows.sort(key=lambda r: r["upload_date"], reverse=True)
    return rows
