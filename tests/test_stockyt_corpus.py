# -*- coding: utf-8 -*-
"""Tests for the deterministic corpus splitter."""

import glob
import json
import os

from agent_reach.stockyt.corpus import split_corpus


def _rec(channel, video_id, status="ok", transcript="some words here"):
    return {
        "channel": channel,
        "video_id": video_id,
        "status": status,
        "transcript": transcript,
        "url": f"https://youtu.be/{video_id}",
    }


def _records():
    return [
        _rec("AuthorA", "v1"),
        _rec("AuthorA", "v2"),
        _rec("AuthorB", "v3"),
        _rec("AuthorC", "v4"),
        _rec("AuthorD", "v5", status="error", transcript=None),  # dropped
        _rec("AuthorD", "v6", transcript=""),                    # dropped (empty)
    ]


def test_authors_json_counts(tmp_path):
    authors, _ = split_corpus(_records(), str(tmp_path), chunks=2)

    on_disk = json.loads((tmp_path / "authors.json").read_text(encoding="utf-8"))
    assert on_disk == authors
    assert authors["N"] == 4  # the error + empty records dropped
    assert authors["total_authors"] == 3
    assert authors["videos_per_author"] == {"AuthorA": 2, "AuthorB": 1, "AuthorC": 1}


def test_chunks_cover_all_records(tmp_path):
    _, num_chunks = split_corpus(_records(), str(tmp_path), chunks=2)
    assert num_chunks == 2

    chunk_files = sorted(glob.glob(os.path.join(str(tmp_path), "chunk_*.jsonl")))
    assert [os.path.basename(p) for p in chunk_files] == ["chunk_01.jsonl", "chunk_02.jsonl"]

    seen = []
    for path in chunk_files:
        for line in open(path, encoding="utf-8"):
            seen.append(json.loads(line)["video_id"])
    assert seen == ["v1", "v2", "v3", "v4"]  # order preserved, error record absent


def test_chunks_clamped_to_record_count(tmp_path):
    # More requested chunks than records → one chunk per record, no empties.
    _, num_chunks = split_corpus(_records(), str(tmp_path), chunks=30)
    assert num_chunks == 4
    for path in glob.glob(os.path.join(str(tmp_path), "chunk_*.jsonl")):
        assert open(path, encoding="utf-8").read().strip()  # non-empty
