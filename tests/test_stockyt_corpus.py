# -*- coding: utf-8 -*-
"""Tests for the deterministic corpus splitter: dedupe by video, chunks sized by transcript characters."""

import glob
import json
import os

from agent_reach.stockyt.corpus import chunk_records, split_corpus


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
        _rec("AuthorA", "v1", transcript="a duplicate row"),     # dropped (same video)
    ]


def _chunk_ids(out_dir):
    paths = sorted(glob.glob(os.path.join(str(out_dir), "chunk_*.jsonl")))
    return [[json.loads(line)["video_id"] for line in open(p, encoding="utf-8")] for p in paths]


def test_authors_json_counts_each_video_once(tmp_path):
    authors, _ = split_corpus(_records(), str(tmp_path), chunk_chars=1000)

    on_disk = json.loads((tmp_path / "authors.json").read_text(encoding="utf-8"))
    assert on_disk == authors
    assert authors["N"] == 4
    assert authors["total_authors"] == 3
    assert authors["videos_per_author"] == {"AuthorA": 2, "AuthorB": 1, "AuthorC": 1}


def test_chunks_cover_all_records_in_order(tmp_path):
    # Each transcript is 15 characters, so 30 characters fit two videos per chunk.
    _, num_chunks = split_corpus(_records(), str(tmp_path), chunk_chars=30)
    assert num_chunks == 2
    assert _chunk_ids(tmp_path) == [["v1", "v2"], ["v3", "v4"]]
    assert json.loads(open(tmp_path / "chunk_01.jsonl").readline())["transcript"] == "some words here"


def test_a_long_video_gets_its_own_chunk_and_is_never_split():
    records = [_rec("A", "short1", transcript="x" * 10), _rec("A", "long", transcript="y" * 100),
               _rec("A", "short2", transcript="z" * 10), _rec("A", "short3", transcript="w" * 10)]
    assert [[r["video_id"] for r in c] for c in chunk_records(records, 25)] == [
        ["short1"], ["long"], ["short2", "short3"]]


def test_no_records_gives_no_chunks(tmp_path):
    authors, num_chunks = split_corpus([], str(tmp_path), chunk_chars=100)
    assert (authors["N"], num_chunks) == (0, 0)


def test_a_shorter_resplit_removes_stale_chunk_files(tmp_path):
    split_corpus(_records(), str(tmp_path), chunk_chars=15)
    assert len(_chunk_ids(tmp_path)) == 4
    split_corpus(_records(), str(tmp_path), chunk_chars=1000)
    assert _chunk_ids(tmp_path) == [["v1", "v2", "v3", "v4"]]
