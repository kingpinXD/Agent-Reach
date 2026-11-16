# -*- coding: utf-8 -*-
"""Tests for the deterministic ticker grader."""

import json

import pytest

from agent_reach.stockyt.cli import main
from agent_reach.stockyt.config import Weights
from agent_reach.stockyt.grading import grade
from agent_reach.stockyt.report import leaderboard_markdown

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
        _mention("AuthorA", None, "v2", "bullish", company=None),  # neither → skipped
        _mention("AuthorA", "", "v3", "bullish", company=""),
    ]
    result = grade(rows, VPA, TOTAL)
    assert list(result["tickers"]) == ["GOOGL"]


def test_null_ticker_row_grades_under_its_company_name():
    rows = [_strong("AuthorA", None, "v1", "bearish")]
    rows[0]["company"] = "SpaceX"
    result = grade(rows, VPA, TOTAL)
    assert list(result["tickers"]) == ["SPACEX"]
    assert [v["ticker"] for v in result["bearish"]] == ["SPACEX"]


def test_a_null_ticker_needs_an_english_company_name_to_be_graded():
    english, devanagari = _strong("AuthorA", None, "v1", "bullish"), _strong("AuthorB", None, "v2", "bullish")
    english["company"], devanagari["company"] = "Tata Capital", "टाटा कैपिटल"
    assert list(grade([english, devanagari], VPA, TOTAL)["tickers"]) == ["TATACAPITAL"]


def test_null_ticker_company_row_merges_with_an_aliased_ticker_row():
    nameless = _strong("AuthorA", None, "v1", "bearish")
    nameless["company"] = "Space-X"
    rows = [nameless, _strong("AuthorB", "SPCX", "v2", "bearish")]
    spacex = grade(rows, VPA, TOTAL)["tickers"]["SPACEX"]
    assert spacex["breadth"] == 2 and spacex["bear_breadth"] == 2


def test_leaderboard_markdown_has_both_boards():
    result = grade(_build_rows(), VPA, TOTAL)
    md = leaderboard_markdown(result)
    assert "## Bullish" in md and "## Bearish" in md
    assert "| Rank | Ticker | Company | Tier | Score | Breadth | Contested | Thesis |" in md
    assert "⚔️" in md  # FIGHT is contested


def test_grade_cli_writes_json_md_and_integrity(tmp_path):
    rows = [_strong("AuthorA", "CONS", "v1", "bullish"), _strong("AuthorB", "CONS", "v2", "bullish")]
    (tmp_path / "extract_01.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    (tmp_path / "chunk_01.jsonl").write_text(
        "\n".join(json.dumps({"video_id": v}) for v in ["v1", "v2"]) + "\n", encoding="utf-8"
    )
    (tmp_path / "extract_01.videos.json").write_text(json.dumps({"v1": "done", "v2": "done"}), encoding="utf-8")
    (tmp_path / "authors.json").write_text(
        json.dumps({"videos_per_author": {"AuthorA": 1, "AuthorB": 1}, "total_authors": 2, "N": 2}),
        encoding="utf-8",
    )

    out = tmp_path / "graded.json"
    assert main(["grade", "--extract-dir", str(tmp_path),
                 "--authors", str(tmp_path / "authors.json"), "-o", str(out)]) == 0

    assert out.exists() and (tmp_path / "graded.md").exists()
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["integrity"] == {"n": 2, "extracted": 2, "missing": []}
    assert "## Bullish" in (tmp_path / "graded.md").read_text(encoding="utf-8")


def test_grade_cli_exits_1_when_a_chunk_video_was_not_extracted(tmp_path, capsys):
    (tmp_path / "extract_01.jsonl").write_text(json.dumps(_strong("AuthorA", "CONS", "v1", "bullish")) + "\n",
                                               encoding="utf-8")
    (tmp_path / "chunk_01.jsonl").write_text(
        "\n".join(json.dumps({"video_id": v}) for v in ["v1", "v2"]) + "\n", encoding="utf-8")
    (tmp_path / "extract_01.videos.json").write_text(json.dumps({"v1": "done", "v2": "skipped"}), encoding="utf-8")
    (tmp_path / "authors.json").write_text(
        json.dumps({"videos_per_author": {"AuthorA": 2}, "total_authors": 1, "N": 2}), encoding="utf-8")
    assert main(["grade", "--extract-dir", str(tmp_path), "--authors", str(tmp_path / "authors.json"),
                 "-o", str(tmp_path / "graded.json")]) == 1
    assert "Integrity: Extract: 1/2 videos [FAIL]" in capsys.readouterr().out


# ── per-side coverage, smoothing, per-side theses and breadth ──

def test_a_bearish_or_neutral_video_does_not_raise_the_bullish_score():
    bullish_only = [_strong("AuthorA", "QQQ", "v1", "bullish"), _strong("AuthorA", "QQQ", "v2", "bullish")]
    with_bearish = bullish_only + [_strong("AuthorA", "QQQ", "v3", "bearish")]
    with_neutral = bullish_only + [_mention("AuthorA", "QQQ", "v4", "neutral", conviction=1)]
    base = grade(bullish_only, VPA, TOTAL)["tickers"]["QQQ"]["bull_score"]
    assert grade(with_bearish, VPA, TOTAL)["tickers"]["QQQ"]["bull_score"] == base
    assert grade(with_neutral, VPA, TOTAL)["tickers"]["QQQ"]["bull_score"] == base


def test_a_channel_with_both_sides_counts_on_each_board_but_is_not_contested_alone():
    rows = [_strong("AuthorA", "QQQ", "v1", "bullish"), _strong("AuthorA", "QQQ", "v2", "bullish"),
            _strong("AuthorA", "QQQ", "v3", "bearish")]
    result = grade(rows, VPA, TOTAL)
    qqq = result["tickers"]["QQQ"]
    assert (qqq["bull_breadth"], qqq["bear_breadth"], qqq["breadth"]) == (1, 1, 1)
    assert qqq["bull_score"] == 2 * qqq["bear_score"]  # two bullish videos, one bearish
    assert ["QQQ"] == [v["ticker"] for v in result["bearish"]]
    assert qqq["contested"] is False  # AuthorA is net bullish; nobody is net bearish


def test_coverage_is_smoothed_by_k():
    one_video = {"AuthorA": 1}
    rows = [_strong("AuthorA", "HNST", "v1", "bullish")]
    full = grade(rows, one_video, 1, Weights(coverage_smoothing_k=0))["tickers"]["HNST"]["bull_score"]
    assert grade(rows, one_video, 1)["tickers"]["HNST"]["bull_score"] == pytest.approx(full / 2)
    assert grade(rows, one_video, 1, Weights(coverage_smoothing_k=3))["tickers"]["HNST"]["bull_score"] == pytest.approx(full / 4)


def test_smoothing_ranks_a_one_video_channel_below_a_steady_bigger_one():
    vpa = {"Small": 1, "Big": 4}
    rows = [_strong("Small", "ONE", "s1", "bullish")]
    rows += [_strong("Big", "STEADY", f"b{i}", "bullish") for i in range(3)]
    assert [v["ticker"] for v in grade(rows, vpa, 2, Weights(coverage_smoothing_k=0))["bullish"]] == ["ONE", "STEADY"]
    assert [v["ticker"] for v in grade(rows, vpa, 2)["bullish"]] == ["STEADY", "ONE"]


def test_top_theses_are_per_side_and_strongest_first():
    weak = _mention("AuthorA", "QQQ", "v1", "bullish", conviction=2,
                    thesis="A weaker bullish thesis without any catalyst.")
    strong = _strong("AuthorB", "QQQ", "v2", "bullish")
    bear = _mention("AuthorC", "QQQ", "v3", "bearish", conviction=3,
                    thesis="Bearish short term on the daily chart.")
    theses = grade([weak, bear, strong], VPA, TOTAL)["tickers"]["QQQ"]["top_theses"]
    assert theses["bullish"] == [("AuthorB", strong["thesis"]), ("AuthorA", weak["thesis"])]
    assert theses["bearish"] == [("AuthorC", bear["thesis"])]


def test_each_board_shows_its_own_thesis_and_side_breadth():
    rows = [
        _strong("AuthorA", "QQQ", "v1", "bullish"),
        _mention("AuthorB", "QQQ", "v2", "bearish", thesis="Bearish short term on the daily chart."),
        _mention("AuthorC", "QQQ", "v3", "neutral", conviction=1),
    ]
    md = leaderboard_markdown(grade(rows, VPA, TOTAL))
    bullish, bearish = md.split("## Bearish")
    assert "| 1 bull · 3/3 |" in bullish and "A genuinely substantive" in bullish
    assert "| 1 bear · 3/3 |" in bearish and "Bearish short term" in bearish
    assert "Bearish short term" not in bullish
