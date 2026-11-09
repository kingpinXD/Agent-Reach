# -*- coding: utf-8 -*-
"""Split a transcripts JSONL corpus into per-author stats + contiguous chunks.

Deterministic, no LLM. Keeps one successfully-transcribed record per video, computes
an authors.json (per-channel video counts), and slices the kept records into
contiguous chunk_*.jsonl files, each about `chunk_chars` of transcript, for ticker extraction.
"""

from __future__ import annotations

import glob
import json
import os
from collections import Counter

from agent_reach.stockyt.run import atomic_write_json, atomic_write_text


def _kept(records: list[dict]) -> list[dict]:
    """The first status=='ok' record with a transcript per video_id, order preserved."""
    kept: dict[str, dict] = {}
    for r in records:
        if r.get("status") == "ok" and r.get("transcript"):
            kept.setdefault(r.get("video_id") or r.get("url"), r)
    return list(kept.values())


def chunk_records(records: list[dict], chunk_chars: int) -> list[list[dict]]:
    """Contiguous groups whose transcripts total at most `chunk_chars`; a video is never split,
    so one longer than `chunk_chars` gets a chunk of its own."""
    chunks: list[list[dict]] = []
    size = 0
    for r in records:
        length = len(r["transcript"])
        if not chunks or size + length > chunk_chars:
            chunks.append([])
            size = 0
        chunks[-1].append(r)
        size += length
    return chunks


def authors_summary(records: list[dict]) -> dict:
    """authors.json payload over the already-kept records.

    {"total_authors": <#distinct channels>, "videos_per_author": {channel: count}, "N": N}.
    """
    counts = Counter(r.get("channel") for r in records)
    return {
        "total_authors": len(counts),
        "videos_per_author": dict(counts),
        "N": len(records),
    }


def split_corpus(records: list[dict], out_dir: str, chunk_chars: int) -> tuple[dict, int]:
    """Write authors.json + contiguous chunk_*.jsonl from a transcripts corpus.

    Keeps one ok record with a transcript per video, writes authors.json, and splits the
    kept records into chunks of about `chunk_chars` transcript characters (full records,
    order preserved). Chunk files left from an earlier, longer split are removed.
    Returns (authors_dict, num_chunks).
    """
    kept = _kept(records)
    authors = authors_summary(kept)
    atomic_write_json(os.path.join(out_dir, "authors.json"), authors)

    chunks = chunk_records(kept, chunk_chars)
    for idx, chunk in enumerate(chunks, 1):
        atomic_write_text(os.path.join(out_dir, f"chunk_{idx:02d}.jsonl"),
                          "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in chunk))
    written = {f"chunk_{idx:02d}.jsonl" for idx in range(1, len(chunks) + 1)}
    for path in glob.glob(os.path.join(out_dir, "chunk_*.jsonl")):
        if os.path.basename(path) not in written:
            os.remove(path)
    return authors, len(chunks)
