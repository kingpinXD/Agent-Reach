# -*- coding: utf-8 -*-
"""Tests for the two-round ticker review: code decides each row's outcome, grade honours it."""

import json

import pytest

from agent_reach.stockyt.cli import main
from agent_reach.stockyt.grading import grade
from agent_reach.stockyt.report import leaderboard_markdown
from agent_reach.stockyt.review import apply_review, review_run


def _row(video_id, ticker, sentiment="bullish", channel="AuthorA", **extra):
    return {"video_id": video_id, "channel": channel, "ticker": ticker, "company": f"{ticker} Inc",
            "asset_type": "stock", "sentiment": sentiment, "conviction": 4, **extra}


def _finding(fid, video_id, ticker, issue, proposed=None):
    return {"id": fid, "video_id": video_id, "ticker": ticker, "issue": issue,
            "proposed": proposed or {}, "quote": "verbatim transcript words"}


def _resp(fid, decision):
    return {"id": fid, "decision": decision, "reason": "because"}


def _apply(extract, r1=(), resp1=(), verdicts=(), r2_findings=(), resp2=()):
    r2 = {"verdicts": [{"id": i, "verdict": v} for i, v in verdicts], "findings": list(r2_findings)}
    return apply_review(list(extract), list(r1), list(resp1), r2, list(resp2))


def _ok(*args, **kwargs):
    rows, problems, disputed_missed = _apply(*args, **kwargs)
    assert problems == []
    return rows, disputed_missed


def test_no_findings_confirms_every_row():
    rows, _ = _ok([_row("v1", "NVDA"), _row("v2", "AMD")])
    assert [(r["review_status"], r["review_note"]) for r in rows] == [("confirmed", []), ("confirmed", [])]


def test_accepted_wrong_ticker_corrects_ticker_company_and_type():
    proposed = {"ticker": "GOOGL", "company": "Alphabet", "asset_type": "stock"}
    rows, _ = _ok([_row("v1", "GOOGLE", asset_type="etf")],
                  [_finding("f1", "v1", "GOOGLE", "wrong_ticker", proposed)], [_resp("f1", "accept")])
    row = rows[0]
    assert (row["ticker"], row["company"], row["asset_type"]) == ("GOOGL", "Alphabet", "stock")
    assert (row["review_status"], row["original_ticker"], row["review_note"]) == ("corrected", "GOOGLE", ["f1"])


def test_accepted_wrong_sentiment_corrects_sentiment():
    rows, _ = _ok([_row("v1", "TSLA", "bullish")],
                  [_finding("f1", "v1", "TSLA", "wrong_sentiment", {"sentiment": "bearish"})],
                  [_resp("f1", "accept")])
    assert (rows[0]["sentiment"], rows[0]["original_sentiment"], rows[0]["review_status"]) == \
        ("bearish", "bullish", "corrected")


@pytest.mark.parametrize("issue", ["not_discussed", "not_investable"])
def test_accepted_drop_issues_drop_the_row(issue):
    rows, _ = _ok([_row("v1", "SPX")], [_finding("f1", "v1", "SPX", issue)], [_resp("f1", "accept")])
    assert rows[0]["review_status"] == "dropped"


def test_accepted_missed_ticker_adds_a_row():
    proposed = _row(None, "PLTR", "bullish", channel="AuthorA")
    rows, _ = _ok([_row("v1", "NVDA")], [_finding("f1", "v1", None, "missed_ticker", proposed)],
                  [_resp("f1", "accept")])
    added = rows[1]
    assert (added["video_id"], added["ticker"], added["review_status"], added["review_note"]) == \
        ("v1", "PLTR", "added", ["f1"])


@pytest.mark.parametrize("issue, proposed", [
    ("wrong_ticker", {"ticker": "AMD"}),
    ("wrong_sentiment", {"sentiment": "bearish"}),
    ("not_discussed", {}),
    ("not_investable", {}),
])
def test_rejected_and_held_finding_disputes_the_row_unchanged(issue, proposed):
    rows, _ = _ok([_row("v1", "NVDA")], [_finding("f1", "v1", "NVDA", issue, proposed)],
                  [_resp("f1", "reject")], verdicts=[("f1", "hold")])
    row = rows[0]
    assert (row["review_status"], row["ticker"], row["sentiment"]) == ("disputed", "NVDA", "bullish")
    assert "original_ticker" not in row and "original_sentiment" not in row


def test_disputed_missed_ticker_adds_no_row_only_a_count():
    rows, disputed_missed = _ok(
        [_row("v1", "NVDA")], [_finding("f1", "v1", None, "missed_ticker", _row(None, "PLTR"))],
        [_resp("f1", "reject")], verdicts=[("f1", "hold")])
    assert len(rows) == 1 and disputed_missed == 1


def test_withdrawn_finding_leaves_row_confirmed():
    rows, _ = _ok([_row("v1", "NVDA")], [_finding("f1", "v1", "NVDA", "not_investable")],
                  [_resp("f1", "reject")], verdicts=[("f1", "withdraw")])
    assert (rows[0]["review_status"], rows[0]["review_note"]) == ("confirmed", ["f1"])


def test_rejected_r2_finding_is_disputed():
    rows, _ = _ok([_row("v1", "NVDA")], r2_findings=[_finding("r2-1", "v1", "NVDA", "wrong_sentiment",
                                                              {"sentiment": "neutral"})],
                  resp2=[_resp("r2-1", "reject")])
    assert rows[0]["review_status"] == "disputed"


def test_finding_ticker_is_normalized_before_matching():
    rows, _ = _ok([_row("v1", "GOOGL")], [_finding("f1", "v1", "goog", "not_discussed")], [_resp("f1", "accept")])
    assert rows[0]["review_status"] == "dropped"


def test_r2_finding_matches_the_ticker_r1_corrected():
    rows, _ = _ok(
        [_row("v1", "FB")],
        [_finding("f1", "v1", "FB", "wrong_ticker", {"ticker": "META", "company": "Meta Platforms"})],
        [_resp("f1", "accept")],
        r2_findings=[_finding("r2-1", "v1", "META", "wrong_sentiment", {"sentiment": "bearish"})],
        resp2=[_resp("r2-1", "accept")],
    )
    row = rows[0]
    assert (row["ticker"], row["sentiment"], row["original_ticker"], row["original_sentiment"]) == \
        ("META", "bearish", "FB", "bullish")
    assert (row["review_status"], row["review_note"]) == ("corrected", ["f1", "r2-1"])


def test_second_correction_keeps_the_first_original_value():
    rows, _ = _ok(
        [_row("v1", "NVDA", "bullish")],
        [_finding("f1", "v1", "NVDA", "wrong_sentiment", {"sentiment": "neutral"})], [_resp("f1", "accept")],
        r2_findings=[_finding("r2-1", "v1", "NVDA", "wrong_sentiment", {"sentiment": "bearish"})],
        resp2=[_resp("r2-1", "accept")],
    )
    assert (rows[0]["sentiment"], rows[0]["original_sentiment"]) == ("bearish", "bullish")


def test_disputed_beats_corrected_and_keeps_the_original_value():
    rows, _ = _ok(
        [_row("v1", "NVDA", "bullish")],
        [_finding("f1", "v1", "NVDA", "wrong_sentiment", {"sentiment": "neutral"}),
         _finding("f2", "v1", "NVDA", "not_investable")],
        [_resp("f1", "accept"), _resp("f2", "reject")],
        verdicts=[("f2", "hold")],
    )
    row = rows[0]
    assert (row["review_status"], row["sentiment"], row["original_sentiment"]) == ("disputed", "neutral", "bullish")


@pytest.mark.parametrize("kwargs, message", [
    ({"r1": [_finding("f1", "v1", "NVDA", "not_investable")]}, "finding f1: no accept/reject response"),
    ({"r1": [_finding("f1", "v1", "NVDA", "not_investable")], "resp1": [_resp("f1", "reject")]},
     "finding f1: rejected in R1 but has no hold/withdraw verdict in R2"),
    ({"r2_findings": [_finding("r2-1", "v1", "NVDA", "not_investable")]}, "finding r2-1: no accept/reject response"),
    ({"r1": [_finding("f1", "v9", "NVDA", "not_investable")], "resp1": [_resp("f1", "accept")]},
     "finding f1: no row for video v9 / NVDA"),
    ({"r1": [_finding("f1", "v1", None, "missed_ticker", _row(None, "NVDA"))], "resp1": [_resp("f1", "accept")]},
     "finding f1: missed_ticker NVDA already exists for video v1"),
    ({"r1": [_finding("f1", "v1", "NVDA", "bad_vibes")], "resp1": [_resp("f1", "accept")]},
     "finding f1: unknown issue 'bad_vibes'"),
    ({"r2_findings": [_finding("x1", "v1", "NVDA", "not_investable")], "resp2": [_resp("x1", "accept")]},
     "R2 finding x1: id must start with 'r2-'"),
])
def test_incomplete_or_inconsistent_review_is_a_problem(kwargs, message):
    _, problems, _ = _apply([_row("v1", "NVDA")], **kwargs)
    assert problems == [message]


# ── review_run: files in a run dir ──

def _write_chunk(run_dir, nn, extract, r1=(), resp1=(), r2=None, resp2=()):
    (run_dir / f"chunk_{nn}.jsonl").write_text(json.dumps({"video_id": "v1"}) + "\n", encoding="utf-8")
    (run_dir / f"extract_{nn}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in extract), encoding="utf-8")
    (run_dir / f"extract_{nn}.videos.json").write_text(json.dumps({"v1": "done"}), encoding="utf-8")
    files = {f"review_{nn}_r1.json": list(r1), f"response_{nn}_r1.json": list(resp1),
             f"review_{nn}_r2.json": r2 or {"verdicts": [], "findings": []}, f"response_{nn}_r2.json": list(resp2)}
    for name, payload in files.items():
        (run_dir / name).write_text(json.dumps(payload), encoding="utf-8")


def test_empty_extract_gets_an_empty_reviewed_file(tmp_path):
    _write_chunk(tmp_path, "01", [])
    counts = review_run(str(tmp_path))
    assert counts["ok"] is True and counts["reviewed_chunks"] == 1
    assert (tmp_path / "reviewed_01.jsonl").read_text(encoding="utf-8") == ""


def test_problem_chunk_writes_nothing_and_fails(tmp_path):
    _write_chunk(tmp_path, "01", [_row("v1", "NVDA")])
    _write_chunk(tmp_path, "02", [_row("v1", "AMD")], r1=[_finding("f1", "v1", "AMD", "not_investable")])
    counts = review_run(str(tmp_path))
    assert counts["ok"] is False
    assert counts["problems"] == ["chunk 02: finding f1: no accept/reject response"]
    assert (tmp_path / "reviewed_01.jsonl").exists()
    assert not (tmp_path / "reviewed_02.jsonl").exists()


def test_missing_review_file_fails_the_chunk(tmp_path):
    _write_chunk(tmp_path, "01", [_row("v1", "NVDA")])
    (tmp_path / "review_01_r2.json").unlink()
    counts = review_run(str(tmp_path))
    assert counts["problems"] == ["chunk 01: missing review_01_r2.json"]
    assert counts["ok"] is False


# ── grade honours the review ──

VPA = {"AuthorA": 5, "AuthorB": 5}


def test_disputed_row_adds_nothing_to_the_bullish_score():
    bullish = _row("v1", "NVDA", "bullish", thesis="A real multi-word thesis about data centres.")
    disputed = _row("v2", "NVDA", "bearish", thesis="Another real thesis about valuation risk.",
                    review_status="disputed")
    with_disputed = grade([bullish, disputed], VPA, 2)
    only_bullish = grade([bullish], VPA, 2)
    assert with_disputed["tickers"]["NVDA"]["bull_score"] == only_bullish["tickers"]["NVDA"]["bull_score"]
    assert with_disputed["tickers"]["NVDA"]["n_videos"] == 1
    assert with_disputed["disputed"] == {"NVDA": 1}


def test_dropped_rows_are_ignored_and_not_counted_as_disputed():
    result = grade([_row("v1", "SPX", review_status="dropped")], VPA, 2)
    assert result["tickers"] == {} and result["disputed"] == {}


def test_ticker_with_only_disputed_rows_is_counted_but_on_neither_board():
    result = grade([_row("v1", "NVDA"), _row("v2", "TSLA", "bearish", review_status="disputed")], VPA, 2)
    assert result["disputed"] == {"TSLA": 1}
    assert "TSLA" not in result["tickers"]
    assert "TSLA" not in [v["ticker"] for v in result["bullish"] + result["bearish"]]


def _run_dir_with_review(tmp_path):
    rows = [_row("v1", "NVDA"), _row("v1", "TSLA", "bearish"), _row("v2", "NVDA", channel="AuthorB")]
    _write_chunk(tmp_path, "01", rows, r1=[_finding("f1", "v1", "TSLA", "wrong_sentiment", {"sentiment": "bullish"})],
                 resp1=[_resp("f1", "reject")], r2={"verdicts": [{"id": "f1", "verdict": "hold"}], "findings": []})
    (tmp_path / "authors.json").write_text(
        json.dumps({"videos_per_author": VPA, "total_authors": 2, "N": 1}), encoding="utf-8")


def test_review_apply_then_grade_reviewed(tmp_path, capsys):
    _run_dir_with_review(tmp_path)
    assert main(["review-apply", "--run-dir", str(tmp_path)]) == 0
    assert "Review: OK" in capsys.readouterr().out

    out = tmp_path / "graded.json"
    assert main(["grade", "--reviewed", "--extract-dir", str(tmp_path),
                 "--authors", str(tmp_path / "authors.json"), "-o", str(out)]) == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["review"]["disputed"] == 1 and result["disputed"] == {"TSLA": 1}
    assert "TSLA" not in result["tickers"]
    md = (tmp_path / "graded.md").read_text(encoding="utf-8")
    assert "| Contested | Disputed | Thesis |" in md
    assert "Review: 2 confirmed · 0 corrected · 0 added · 0 dropped · 1 disputed" in md
    assert "TSLA 1" in md


def test_unreviewed_markdown_has_no_disputed_column():
    md = leaderboard_markdown(grade([_row("v1", "NVDA")], VPA, 2))
    assert "Disputed" not in md and "Review:" not in md


def test_review_apply_fails_on_problems(tmp_path, capsys):
    _write_chunk(tmp_path, "01", [_row("v1", "AMD")], r1=[_finding("f1", "v1", "AMD", "not_investable")])
    assert main(["review-apply", "--run-dir", str(tmp_path)]) == 1
    assert "Review: FAIL" in capsys.readouterr().out


def test_grade_reviewed_fails_without_a_reviewed_file_per_extract(tmp_path, capsys):
    _run_dir_with_review(tmp_path)
    assert main(["review-apply", "--run-dir", str(tmp_path)]) == 0
    (tmp_path / "extract_02.jsonl").write_text("", encoding="utf-8")
    out = tmp_path / "graded.json"
    assert main(["grade", "--reviewed", "--extract-dir", str(tmp_path),
                 "--authors", str(tmp_path / "authors.json"), "-o", str(out)]) == 1
    assert "missing reviewed_02.jsonl" in capsys.readouterr().err
    assert not out.exists()


def test_grade_reviewed_fails_when_review_apply_did_not_pass(tmp_path, capsys):
    _write_chunk(tmp_path, "01", [_row("v1", "AMD")], r1=[_finding("f1", "v1", "AMD", "not_investable")])
    (tmp_path / "authors.json").write_text(
        json.dumps({"videos_per_author": VPA, "total_authors": 2, "N": 1}), encoding="utf-8")
    main(["review-apply", "--run-dir", str(tmp_path)])
    assert main(["grade", "--reviewed", "--extract-dir", str(tmp_path),
                 "--authors", str(tmp_path / "authors.json"), "-o", str(tmp_path / "g.json")]) == 1
    assert "not ok" in capsys.readouterr().err
