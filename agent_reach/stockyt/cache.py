# -*- coding: utf-8 -*-
"""Per-channel JSONL cache of seen videos.

One file per channel at `<cache_dir>/<channel_id>.jsonl`, each line a row
{video_id, upload_date, title, channel_id, date_source}. `date_source` is "listing" (the flat
listing's date, maybe a day late) or "video" (checked on the video itself); a row without one
reads as "listing". Merges are a lossless union — we never drop a previously cached video.

The cache is only a fallback: the channels stage builds its window from a fresh listing and
reads the cache only when that listing fails.
"""

from __future__ import annotations

import json
import os

from agent_reach.stockyt.run import atomic_write_text


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

    Existing rows win on conflict, except that a video-checked row replaces any cached
    row. The file is rewritten sorted by upload_date descending (newest first).
    """
    merged = load(channel_id, cache_dir)
    added = 0
    for row in new_rows:
        if row["video_id"] not in merged:
            added += 1
        elif row.get("date_source") != "video":
            continue
        merged[row["video_id"]] = row

    ordered = sorted(merged.values(), key=lambda r: r["upload_date"], reverse=True)
    atomic_write_text(_cache_file(channel_id, cache_dir),
                      "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ordered))
    return added, len(ordered)


def all_in_window(channel_id: str, cutoff: str, cache_dir: str, until: str | None = None) -> list[dict]:
    """Cached rows with cutoff <= upload_date (<= until, when given), as YYYYMMDD; newest first."""
    rows = [r for r in load(channel_id, cache_dir).values()
            if r["upload_date"] >= cutoff and (until is None or r["upload_date"] <= until)]
    rows.sort(key=lambda r: r["upload_date"], reverse=True)
    return rows
