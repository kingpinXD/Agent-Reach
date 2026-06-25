# -*- coding: utf-8 -*-
"""Tests for the deterministic ticker grader."""

import json
from unittest.mock import patch

from agent_reach.cli import main
from agent_reach.stockyt.grading import grade, integrity, leaderboard_markdown

VPA = {"AuthorA": 5, "AuthorB": 5, "AuthorC": 5}
TOTAL = 3


def _mention(channel, ticker, video_id, sentiment, **extra):
    row = {
        "channel": channel,
        "ticker": ticker,
        "video_id": video_id,
        "sentiment": sentiment,
        "company": f"{ticker} Inc",
        "asset_type": "stock",
        "conviction": extra.pop("conviction", 4),
    }
    row.update(extra)
    return row


def _strong(channel, ticker, video_id, sentiment):
    return _mention(
        channel, ticker, video_id, sentiment,
        thesis="A genuinely substantive multi-word investment thesis here.",
        catalyst="Earnings beat next quarter",
        price_target="$200",
        is_primary_topic=True,
        time_horizon="medium",
    )


def _build_rows():
    rows = []
    # CONS: two authors agree bullish across distinct videos → breadth boost.
    rows += [_strong("AuthorA", "CONS", "v1", "bullish"), _strong("AuthorB", "CONS", "v2", "bullish")]
    # DROP: a single bare name-drop, near-zero context weight.
    rows += [_mention("AuthorA", "DROP", "v3", "neutral", conviction=1, thesis=None)]
    # FIGHT: one author bullish, one bearish → contested.
    rows += [_strong("AuthorA", "FIGHT", "v4", "bullish"), _strong("AuthorB", "FIGHT", "v5", "bearish")]
    return rows


def test_consensus_outranks_namedrop():
    result = grade(_build_rows(), VPA, TOTAL)
    bull_order = [v["ticker"] for v in result["bullish"]]
    assert bull_order[0] == "CONS"  # two-author consensus tops the board
    cons = result["tickers"]["CONS"]
    assert cons["breadth"] == 2
    # the bare name-drop has near-zero context and shouldn't outrank consensus
    drop = result["tickers"]["DROP"]
    assert drop["avg_context"] < cons["avg_context"]


def test_contested_flag_and_both_boards():
    result = grade(_build_rows(), VPA, TOTAL)
    fight = result["tickers"]["FIGHT"]
    assert fight["contested"] is True
    assert "FIGHT" in [v["ticker"] for v in result["bullish"]]
    assert "FIGHT" in [v["ticker"] for v in result["bearish"]]


def test_tier_and_normalization():
    result = grade(_build_rows(), VPA, TOTAL)
    top = result["bullish"][0]
    assert top["score"] == 100.0  # leader normalized to 100
    assert top["tier"] == "S"


def test_alias_and_missing_ticker():
    rows = [
        _strong("AuthorA", "GOOG", "v1", "bullish"),  # aliased to GOOGL
        _mention("AuthorA", None, "v2", "bullish"),   # no ticker → skipped
    ]
    result = grade(rows, VPA, TOTAL)
    assert "GOOGL" in result["tickers"]
    assert "GOOG" not in result["tickers"]


def test_leaderboard_markdown_has_both_boards():
    result = grade(_build_rows(), VPA, TOTAL)
    md = leaderboard_markdown(result)
    assert "## Bullish" in md and "## Bearish" in md
    assert "| Rank | Ticker | Company | Tier | Score | Breadth | Contested | Thesis |" in md
    assert "⚔️" in md  # FIGHT is contested


def test_integrity_skipped_without_chunks(tmp_path):
    assert integrity(str(tmp_path), 5) is None


def test_integrity_reconciles_chunks_and_extracts(tmp_path):
    (tmp_path / "chunk_01.jsonl").write_text(
        "\n".join(json.dumps({"video_id": v}) for v in ["v1", "v2", "v3"]) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "extract_01.jsonl").write_text(
        json.dumps({"video_id": "v1", "ticker": "AAPL"}) + "\n", encoding="utf-8"
    )
    integ = integrity(str(tmp_path), 3)
    assert integ == {"n": 3, "covered": 3, "with_tickers": 1, "missing": 0, "ok": True}

    # N greater than covered → missing flagged.
    integ = integrity(str(tmp_path), 5)
    assert integ["missing"] == 2 and integ["ok"] is False


def test_grade_cli_writes_json_md_and_integrity(tmp_path):
    rows = [_strong("AuthorA", "CONS", "v1", "bullish"), _strong("AuthorB", "CONS", "v2", "bullish")]
    (tmp_path / "extract_01.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    (tmp_path / "chunk_01.jsonl").write_text(
        "\n".join(json.dumps({"video_id": v}) for v in ["v1", "v2"]) + "\n", encoding="utf-8"
    )
    (tmp_path / "authors.json").write_text(
        json.dumps({"videos_per_author": {"AuthorA": 1, "AuthorB": 1}, "total_authors": 2, "N": 2}),
        encoding="utf-8",
    )

    out = tmp_path / "graded.json"
    argv = ["agent-reach", "grade", "--extract-dir", str(tmp_path),
            "--authors", str(tmp_path / "authors.json"), "-o", str(out)]
    with patch("sys.argv", argv):
        main()

    assert out.exists() and (tmp_path / "graded.md").exists()
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["integrity"] == {"n": 2, "covered": 2, "with_tickers": 2, "missing": 0, "ok": True}
    assert "## Bullish" in (tmp_path / "graded.md").read_text(encoding="utf-8")
