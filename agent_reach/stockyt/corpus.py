# -*- coding: utf-8 -*-
"""Split a transcripts JSONL corpus into per-author stats + contiguous chunks.

Deterministic, no LLM. Keeps only successfully-transcribed records, computes
an authors.json (per-channel video counts), and slices the kept records into
contiguous chunk_*.jsonl files for parallel ticker extraction.
"""

from __future__ import annotations

import json
import os
from collections import Counter


def _kept(records: list[dict]) -> list[dict]:
    """Records with status=='ok' and a non-empty transcript, order preserved."""
    return [r for r in records if r.get("status") == "ok" and r.get("transcript")]


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


def split_corpus(records: list[dict], out_dir: str, chunks: int = 30) -> tuple[dict, int]:
    """Write authors.json + contiguous chunk_*.jsonl from a transcripts corpus.

    Keeps status=='ok' records with a non-empty transcript, writes authors.json,
    and splits them into A = min(chunks, N) contiguous chunk files (full records,
    order preserved). Returns (authors_dict, num_chunks).
    """
    os.makedirs(out_dir, exist_ok=True)
    kept = _kept(records)
    authors = authors_summary(kept)
    n = authors["N"]

    with open(os.path.join(out_dir, "authors.json"), "w", encoding="utf-8") as f:
        json.dump(authors, f, ensure_ascii=False, indent=2)

    num_chunks = min(chunks, n) if n else 0
    for idx in range(num_chunks):
        # Contiguous slices: distribute the remainder across the first chunks.
        start = idx * n // num_chunks
        end = (idx + 1) * n // num_chunks
        path = os.path.join(out_dir, f"chunk_{idx + 1:02d}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for row in kept[start:end]:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return authors, num_chunks
