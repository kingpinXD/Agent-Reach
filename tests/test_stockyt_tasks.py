# -*- coding: utf-8 -*-
"""Tests for the extract and review task templates, schemas and validators."""

import copy
from pathlib import Path

import pytest

from agent_reach.stockyt.llm.schema import schema_errors
from agent_reach.stockyt.review import apply_review
from agent_reach.stockyt.tasks import extract
from agent_reach.stockyt.tasks import review as rt

FIXTURES = Path(__file__).parent / "fixtures" / "stockyt"

RECORDS = [
    {"video_id": "v1", "channel": "Stock Moe", "transcript":
        "Nvidia is my number one pick, I am loading up before earnings. "
        "Tesla, Google, Apple are the bigger stocks. My guest thinks Apple will crash."},
    {"video_id": "v2", "channel": "Meet Kevin", "transcript":
        "Today we talk about the Fed and rates. No single stock today."},
]


def _row(ticker, **extra):
    row = {
        "video_id": "v1", "channel": "Stock Moe", "ticker": ticker, "company": f"{ticker} Inc",
        "asset_type": "stock", "mentions": 1, "sentiment": "neutral", "conviction": 1,
        "is_primary_topic": False, "recommendation": "none", "thesis": None, "catalyst": None,
        "time_horizon": "unspecified", "price_target": None,
        "quote": "Tesla, Google, Apple are the bigger stocks.",
    }
    row.update(extra)
    return row


ROWS = [
    _row("NVDA", sentiment="bullish", conviction=5, recommendation="strong_buy",
         thesis="Loading up before earnings, it is the number one pick.", catalyst="earnings",
         quote="Nvidia is my number one pick, I am loading up before earnings."),
    _row("TSLA"),
    _row("AAPL", sentiment="bearish"),
]

EXTRACT_ANSWER = {"videos": [
    {"video_id": "v1", "status": "done", "rows": ROWS},
    {"video_id": "v2", "status": "no_tickers", "rows": []},
]}


def _finding(fid, ticker, issue, proposed=None, quote="My guest thinks Apple will crash.",
             video_id="v1"):
    return {"id": fid, "video_id": video_id, "ticker": ticker, "issue": issue,
            "proposed": proposed or {}, "quote": quote}


R1 = [
    _finding("f1", "AAPL", "wrong_sentiment", {"sentiment": "neutral"}),
    _finding("f2", "TSLA", "not_discussed", quote="Tesla, Google, Apple are the bigger stocks."),
    _finding("f3", None, "missed_ticker", _row("GOOGL"),
             quote="Tesla, Google, Apple are the bigger stocks."),
]
RESP1 = [
    {"id": "f1", "decision": "accept", "quote": None, "reason": "The guest's view."},
    {"id": "f2", "decision": "reject", "quote": "Tesla, Google, Apple are the bigger stocks.",
     "reason": "A name-drop is said in the video."},
    {"id": "f3", "decision": "accept", "quote": None, "reason": "Missed it."},
]
R2 = {"verdicts": [{"id": "f2", "verdict": "withdraw"}],
      "findings": [_finding("r2-1", "GOOGL", "wrong_sentiment", {"sentiment": "bullish"},
                            quote="Tesla, Google, Apple are the bigger stocks.")]}
RESP2 = [{"id": "r2-1", "decision": "reject", "quote": "Tesla, Google, Apple are the bigger stocks.",
          "reason": "Only a name-drop."}]


def _check(job, answer):
    task, validate = job
    return schema_errors(answer, task.schema) or validate(answer)


# ── extract ──

def test_extract_accepts_a_valid_answer_and_flattens_it_to_rows():
    assert _check(extract.job("e01", RECORDS), EXTRACT_ANSWER) == []
    assert extract.to_rows(EXTRACT_ANSWER) == ROWS


def _broken(mutate):
    answer = copy.deepcopy(EXTRACT_ANSWER)
    mutate(answer)
    return extract.validate(answer, RECORDS)


@pytest.mark.parametrize("mutate, expected", [
    (lambda a: a["videos"].pop(), "video v2: missing from the answer"),
    (lambda a: a["videos"].append(copy.deepcopy(a["videos"][1])), "video v2: answered more than once"),
    (lambda a: a["videos"].append({"video_id": "v9", "status": "no_tickers", "rows": []}),
     "videos[2] (v9): not a video of this chunk"),
    (lambda a: a["videos"][1].update(status="done"), "videos[1] (v2): status 'done' does not match 0 rows"),
    (lambda a: a["videos"][0].update(status="no_tickers"),
     "videos[0] (v1): status 'no_tickers' does not match 3 rows"),
    (lambda a: a["videos"][0]["rows"][1].update(channel="Meet Kevin"),
     "videos[0] (v1).rows[1]: channel 'Meet Kevin' is not 'Stock Moe'"),
    (lambda a: a["videos"][0]["rows"][1].update(video_id="v2"),
     "videos[0] (v1).rows[1]: video_id 'v2' is not 'v1'"),
    (lambda a: a["videos"][0]["rows"][1].update(quote="Tesla to the moon."),
     "videos[0] (v1).rows[1]: quote is not in the transcript"),
    (lambda a: a["videos"][0]["rows"][1].update(ticker="nvda"),
     "videos[0] (v1): ticker NVDA has more than one row"),
    (lambda a: a["videos"][0]["rows"][1].update(conviction=6),
     "videos[0] (v1).rows[1].conviction: 6 is not one of"),
])
def test_extract_validator_catches(mutate, expected):
    errors = _broken(mutate)
    assert len(errors) == 1 and errors[0].startswith(expected), errors


def test_extract_schema_rejects_a_bad_enum():
    answer = copy.deepcopy(EXTRACT_ANSWER)
    answer["videos"][0]["rows"][0]["sentiment"] = "moon"
    assert schema_errors(answer, extract.SCHEMA) == [
        "answer.videos[0].rows[0].sentiment: 'moon' is not one of ['bullish', 'bearish', 'neutral']"]


# ── review: valid exchange, file shapes, apply ──

def test_a_full_valid_exchange_validates_and_apply_review_consumes_its_files():
    rows_after = rt.rows_after_r1(ROWS, R1, RESP1)
    assert _check(rt.r1_review_job("r1", RECORDS, ROWS), {"findings": R1}) == []
    assert _check(rt.response_job("p1", RECORDS, ROWS, R1, False), {"responses": RESP1}) == []
    assert _check(rt.r2_review_job("r2", RECORDS, ROWS, R1, RESP1), R2) == []
    assert _check(rt.response_job("p2", RECORDS, rows_after, R2["findings"], True),
                  {"responses": RESP2}) == []

    files = [rt.to_file({"findings": R1}), rt.to_file({"responses": RESP1}), rt.to_file(R2),
             rt.to_file({"responses": RESP2})]
    assert files == [R1, RESP1, R2, RESP2]
    reviewed, problems, _ = apply_review(ROWS, *files)
    assert problems == []
    status = {r["ticker"]: r["review_status"] for r in reviewed}
    assert status == {"NVDA": "confirmed", "TSLA": "confirmed", "AAPL": "corrected",
                      "GOOGL": "disputed"}


def test_rows_after_r1_apply_only_accepted_findings():
    rows = {r["ticker"]: r for r in rt.rows_after_r1(ROWS, R1, RESP1)}
    assert rows["AAPL"]["sentiment"] == "neutral" and rows["AAPL"]["original_sentiment"] == "bearish"
    assert rows["GOOGL"]["review_status"] == "added"
    assert rows["TSLA"]["review_status"] == "confirmed"  # rejected, not yet judged


# ── review: R1 findings ──

def _r1_errors(findings):
    return rt.validate_r1_review({"findings": findings}, RECORDS, ROWS)


@pytest.mark.parametrize("finding, expected", [
    ({**R1[0], "id": "x1"}, "finding id 'x1' is not like f\\d+"),
    ({**R1[0], "video_id": "v9"}, "finding f1: video 'v9' is not in this chunk"),
    ({**R1[0], "ticker": "MSFT"}, "finding f1: no extract row for v1 / MSFT"),
    ({**R1[0], "quote": "Apple will go to zero."}, "finding f1: quote is not in the transcript"),
    ({**R1[0], "proposed": {"sentiment": "moon"}}, "finding f1: wrong_sentiment needs a proposed sentiment"),
    (_finding("f1", "AAPL", "wrong_ticker", {"company": "Apple"}),
     "finding f1: wrong_ticker needs a proposed ticker"),
    ({**R1[2], "proposed": _row("NVDA")}, "finding f3: NVDA already has a row in v1"),
    ({**R1[2], "proposed": _row("GOOGL", channel="Meet Kevin")},
     "finding f3: proposed: channel 'Meet Kevin' is not 'Stock Moe'"),
    ({**R1[2], "proposed": {"ticker": "GOOGL"}}, "finding f3: proposed: missing 'video_id'"),
])
def test_r1_validator_catches(finding, expected):
    errors = _r1_errors([finding])
    assert errors and errors[0].startswith(expected), errors


def test_r1_validator_catches_a_repeated_id():
    assert _r1_errors([R1[0], R1[0]]) == ["finding id 'f1' is used more than once"]


def test_r1_finding_matches_its_row_through_ticker_aliases():
    rows = ROWS + [_row("GOOGL", quote="Tesla, Google, Apple are the bigger stocks.")]
    finding = _finding("f1", "GOOG", "wrong_sentiment", {"sentiment": "bullish"},
                       quote="Tesla, Google, Apple are the bigger stocks.")
    assert rt.validate_r1_review({"findings": [finding]}, RECORDS, rows) == []


# ── review: responses ──

def _resp_errors(responses, findings=R1):
    return rt.validate_response({"responses": responses}, RECORDS, findings)


def test_response_validator_needs_exactly_one_answer_per_finding():
    assert _resp_errors(RESP1[:2]) == ["finding 'f3' has no response"]
    assert _resp_errors(RESP1 + [RESP1[0]]) == ["finding 'f1' answered more than once"]
    assert _resp_errors(RESP1 + [{**RESP1[0], "id": "f9"}]) == ["response to unknown finding 'f9'"]


def test_response_validator_needs_a_real_quote_for_a_rejection():
    assert _resp_errors([RESP1[0], {**RESP1[1], "quote": None}, RESP1[2]]) == [
        "response f2: a rejection needs a quote"]
    assert _resp_errors([RESP1[0], {**RESP1[1], "quote": "Tesla is a scam."}, RESP1[2]])[0].startswith(
        "response f2: quote is not in the transcript")


def test_response_quote_is_checked_against_its_findings_video():
    finding = _finding("f1", "AAPL", "wrong_sentiment", {"sentiment": "neutral"})
    other_video_quote = "Today we talk about the Fed and rates."
    errors = _resp_errors([{"id": "f1", "decision": "reject", "quote": other_video_quote,
                            "reason": "x"}], [finding])
    assert errors and errors[0].startswith("response f1: quote is not in the transcript")


# ── review: R2 ──

def _r2_errors(answer):
    return rt.validate_r2_review(answer, RECORDS, rt.rows_after_r1(ROWS, R1, RESP1), R1, RESP1)


def test_r2_validator_needs_one_verdict_per_rejected_finding_only():
    assert _r2_errors({"verdicts": [], "findings": []}) == ["rejected finding 'f2' has no verdict"]
    assert _r2_errors({"verdicts": R2["verdicts"] + [{"id": "f1", "verdict": "hold"}], "findings": []}) == [
        "verdict for 'f1', which was not rejected"]
    assert _r2_errors({"verdicts": R2["verdicts"] * 2, "findings": []}) == [
        "finding 'f2' has more than one verdict"]


def test_r2_findings_use_r2_ids_and_target_rows_as_they_stand_after_r1():
    assert _r2_errors({**R2, "findings": [{**R2["findings"][0], "id": "f4"}]}) == [
        "finding id 'f4' is not like r2-\\d+"]
    # GOOGL exists only because R1 added it: R2 may target it, but not a row that never existed.
    assert _r2_errors(R2) == []
    bad = {**R2["findings"][0], "ticker": "MSFT"}
    assert _r2_errors({**R2, "findings": [bad]}) == ["finding r2-1: no extract row for v1 / MSFT"]


# ── rendered prompts (golden files) ──

def _rendered(job):
    task, _ = job
    return f"SYSTEM: {task.system}\n\n{task.prompt}\n"


GOLDEN = {
    "prompt_extract.txt": lambda: extract.job("e01", RECORDS),
    "prompt_review_r1.txt": lambda: rt.r1_review_job("r1", RECORDS, ROWS),
    "prompt_response_r1.txt": lambda: rt.response_job("p1", RECORDS, ROWS, R1, False),
    "prompt_review_r2.txt": lambda: rt.r2_review_job("r2", RECORDS, ROWS, R1, RESP1),
    "prompt_response_r2.txt": lambda: rt.response_job(
        "p2", RECORDS, rt.rows_after_r1(ROWS, R1, RESP1), R2["findings"], True),
}


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_rendered_prompt_matches_golden(name):
    """To accept a deliberate prompt change, delete the golden file and run the test twice."""
    path = FIXTURES / name
    rendered = _rendered(GOLDEN[name]())
    if not path.exists():
        path.write_text(rendered, encoding="utf-8")
        pytest.fail(f"wrote missing golden {path}; review it and re-run")
    assert rendered == path.read_text(encoding="utf-8")


def test_prompts_carry_the_n2_rules():
    extract_prompt = extract.job("e01", RECORDS)[0].prompt
    assert "Use the host's stance; a guest's view is neutral unless the host adopts it." in extract_prompt
    review_prompt = rt.r1_review_job("r1", RECORDS, ROWS)[0].prompt
    assert "`not_discussed` (the ticker is never said in the video" in review_prompt


def test_india_prompts_carry_the_nse_and_language_rule():
    prompts = {
        "extract": extract.job("e01", RECORDS, "india")[0].prompt,
        "review_r1": rt.r1_review_job("r1", RECORDS, ROWS, "india")[0].prompt,
        "response_r1": rt.response_job("p1", RECORDS, ROWS, R1, False, "india")[0].prompt,
        "review_r2": rt.r2_review_job("r2", RECORDS, ROWS, R1, RESP1, "india")[0].prompt,
    }
    for name, prompt in prompts.items():
        assert "NSE symbol (Reliance→RELIANCE, HDFC Bank→HDFCBANK, Infosys→INFY, Tata Motors→TATAMOTORS)" in prompt, name
        assert "NIFTY, BANKNIFTY and SENSEX" in prompt, name
        assert "write `company`, `thesis` and `catalyst` in English" in prompt, name
        assert "copy `quote` verbatim in the transcript's own script" in prompt, name
    assert "NSE" not in extract.job("e01", RECORDS)[0].prompt
    with pytest.raises(KeyError):
        extract.job("e01", RECORDS, "uk")
