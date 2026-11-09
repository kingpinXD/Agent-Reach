# -*- coding: utf-8 -*-
"""Tests for report.py: the review changes, the report files and the console lines."""

import json

from agent_reach.stockyt import report
from agent_reach.stockyt.grading import grade
from agent_reach.stockyt.review import apply_review

# The column layout of the earlier runs' tickers-*.md tables; runs are compared side by side.
REVIEWED_HEADER = "| Rank | Ticker | Company | Tier | Score | Breadth | Contested | Disputed | Thesis |"


def _row(video_id, ticker, sentiment="bullish", channel="Alpha", **extra):
    return {"video_id": video_id, "channel": channel, "ticker": ticker, "company": f"{ticker} Inc",
            "asset_type": "stock", "sentiment": sentiment, "conviction": 4, "is_primary_topic": True,
            "thesis": "A real multi-word thesis about the business.", "catalyst": None, "time_horizon": "long",
            "price_target": None, "quote": "q", **extra}


def _finding(fid, video_id, ticker, issue, proposed=None, quote="the quote"):
    return {"id": fid, "video_id": video_id, "ticker": ticker, "issue": issue, "proposed": proposed or {},
            "quote": quote}


def _resp(fid, decision):
    return {"id": fid, "decision": decision, "quote": "q", "reason": "r"}


def _changes(extract, r1, resp1, r2, resp2):
    reviewed, problems, _ = apply_review(extract, r1, resp1, r2, resp2)
    assert problems == []
    return report.chunk_changes(r1, resp1, r2, resp2, reviewed)


def test_chunk_changes_lists_accepted_and_disputed_findings_with_their_row():
    extract = [_row("v1", "TSLA"), _row("v1", "GOOG", channel="Alpha"), _row("v2", "AMD", channel="Beta"),
               _row("v2", "SPX", channel="Beta"), _row("v3", "ETH", "bearish", channel="Gamma")]
    added = _row("v2", "AMZN", channel="Beta")
    r1 = [
        _finding("f1", "v1", "TSLA", "wrong_sentiment", {"sentiment": "bearish"}, quote="I am selling"),
        _finding("f2", "v1", "GOOG", "wrong_ticker", {"ticker": "GOOGL"}),
        _finding("f3", "v2", "SPX", "not_investable"),
        _finding("f4", "v2", None, "missed_ticker", added),
        _finding("f5", "v2", "AMD", "not_discussed"),          # rejected, then withdrawn: no change
        _finding("f6", "v3", "ETH", "wrong_sentiment", {"sentiment": "neutral"}),  # rejected and held
    ]
    resp1 = [_resp("f1", "accept"), _resp("f2", "accept"), _resp("f3", "accept"), _resp("f4", "accept"),
             _resp("f5", "reject"), _resp("f6", "reject")]
    r2 = {"verdicts": [{"id": "f5", "verdict": "withdraw"}, {"id": "f6", "verdict": "hold"}],
          "findings": [_finding("r2-1", "v2", "AMZN", "wrong_sentiment", {"sentiment": "bearish"})]}
    resp2 = [_resp("r2-1", "reject")]

    changes = _changes(extract, r1, resp1, r2, resp2)
    assert [(c["finding"], c["channel"], c["ticker"], c["status"], c["original"], c["new"]) for c in changes] == [
        ("f1", "Alpha", "TSLA", "corrected", "bullish", "bearish"),
        ("f2", "Alpha", "GOOG", "corrected", "GOOG", "GOOGL"),
        ("f3", "Beta", "SPX", "dropped", "SPX", "dropped"),
        ("f4", "Beta", "AMZN", "added", None, "AMZN bullish"),
        ("f6", "Gamma", "ETH", "disputed", "bearish", "neutral"),
        ("r2-1", "Beta", "AMZN", "disputed", "bullish", "bearish"),  # an rN-2 rejection has no round 3
    ]
    assert changes[0]["quote"] == "I am selling" and changes[0]["video_id"] == "v1"


def test_a_disputed_missed_ticker_is_listed_from_its_proposal():
    proposed = _row("v1", "AMZN", channel="Beta")
    r1 = [_finding("f1", "v1", None, "missed_ticker", proposed)]
    changes = _changes([], r1, [_resp("f1", "reject")], {"verdicts": [{"id": "f1", "verdict": "hold"}],
                                                         "findings": []}, [])
    assert [(c["channel"], c["ticker"], c["status"]) for c in changes] == [("Beta", "AMZN", "disputed")]


def test_review_changes_reads_each_reviewed_chunk(tmp_path):
    extract = [_row("v1", "TSLA")]
    r1 = [_finding("f1", "v1", "TSLA", "wrong_sentiment", {"sentiment": "bearish"})]
    resp1 = [_resp("f1", "accept")]
    reviewed, _, _ = apply_review(extract, r1, resp1, {"verdicts": [], "findings": []}, [])
    files = {"review_01_r1.json": r1, "response_01_r1.json": resp1,
             "review_01_r2.json": {"verdicts": [], "findings": []}, "response_01_r2.json": []}
    for name, payload in files.items():
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "reviewed_01.jsonl").write_text("".join(json.dumps(r) + "\n" for r in reviewed), encoding="utf-8")
    assert [c["finding"] for c in report.review_changes(str(tmp_path))] == ["f1"]


def test_changes_markdown_is_a_table_or_a_plain_line():
    assert "Nothing: every row was confirmed." in report.changes_markdown([])
    md = report.changes_markdown([{"channel": "A", "video_id": "v1", "ticker": "TSLA", "status": "corrected",
                                   "original": "bullish", "new": "bearish", "quote": "a | b"}])
    assert "| Channel | Video | Ticker | Status | Change | Quote |" in md
    assert "| A | [v1](https://www.youtube.com/watch?v=v1) | TSLA | corrected | bullish → bearish | a / b |" in md


def _graded():
    rows = [_row("v1", "NVDA", review_status="confirmed"), _row("v2", "TSLA", "bearish", channel="Beta",
                                                                review_status="corrected")]
    result = grade(rows, {"Alpha": 2, "Beta": 1}, 2)
    result["review"] = {"confirmed": 1, "corrected": 1, "added": 0, "dropped": 0, "disputed": 0,
                        "disputed_missed": 0}
    return result


def test_write_keeps_the_leaderboard_layout_and_adds_the_changes(tmp_path):
    change = {"channel": "Beta", "video_id": "v2", "ticker": "TSLA", "status": "corrected",
              "original": "bullish", "new": "bearish", "quote": "q", "finding": "f1"}
    json_path, md_path = report.write(str(tmp_path), _graded(), [change])

    saved = json.loads(open(json_path, encoding="utf-8").read())
    assert saved["review_changes"] == [change] and saved["bullish"][0]["ticker"] == "NVDA"
    md = open(md_path, encoding="utf-8").read()
    assert md.count(REVIEWED_HEADER) == 2
    assert md.index("## Bullish") < md.index("## Bearish") < md.index("## What the review changed")


def test_console_lines_show_each_sides_breadth():
    lines = report.console_lines(_graded())
    assert " 1. NVDA   S 100.0 | 1 bull · 1/2 auth | NVDA Inc" in lines
    assert " 1. TSLA   S 100.0 | 1 bear · 1/2 auth | TSLA Inc" in lines
