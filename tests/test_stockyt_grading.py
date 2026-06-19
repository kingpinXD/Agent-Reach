# -*- coding: utf-8 -*-
"""Tests for the deterministic ticker grader."""

from agent_reach.stockyt.grading import grade

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
