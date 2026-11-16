# -*- coding: utf-8 -*-
"""`agent-reach stockyt run` end to end, on recorded yt-dlp output and canned LLM answers.

Three channels, five videos in the window, one cutoff-day row that the video's real date
excludes, and one caption call that YouTube rate limits until cookies are sent. The answers
cover an accepted correction, a rejection held (disputed), a rejection withdrawn, and an
accepted missed ticker.
"""

import json
from pathlib import Path

import pytest

from agent_reach.stockyt import config as stockyt_config
from agent_reach.stockyt import pipeline, youtube
from agent_reach.stockyt.cli import main
from agent_reach.stockyt.upstream import RATE_LIMITED

GOLDEN = Path(__file__).parent / "fixtures" / "stockyt" / "e2e_report.md"
SINCE = "20261105"
CHANNELS = [("Alpha", "UCalpha"), ("Beta", "UCbeta"), ("Gamma", "UCgamma")]

TRANSCRIPTS = {
    "a1": ("Alpha", "Nvidia is my top pick because data center demand keeps growing. "
                    "I am loading up on NVDA before earnings."),
    "a2": ("Alpha", "Tesla looks weak to me. Deliveries are falling and I am selling my shares."),
    "b1": ("Beta", "Apple is fine but I prefer Microsoft for the cloud growth. Microsoft is a buy for me."),
    "b2": ("Beta", "Palantir keeps winning government contracts and I think it can double from here. "
                   "I also bought more Amazon for the AWS business."),
    "g1": ("Gamma", "I am bearish on bitcoin because the weekly trend broke. Bitcoin could fall to fifty "
                    "thousand. Ethereum is also heading lower."),
}
UPLOADED = {"a1": "20261107", "a2": "20261106", "b1": "20261107", "b2": "20261106", "g1": "20261106"}


def _url(video_id):
    return youtube.watch_url(video_id)


def _captions(video_id):
    channel, text = TRANSCRIPTS[video_id]
    channel_id = dict(CHANNELS)[channel]
    vtt = f"WEBVTT\n\n00:00:00.000 --> 00:00:09.000\n{text}\n"
    return {"stdout": f"{channel}\t{channel_id}\t{UPLOADED[video_id]}\t1762500000\n",
            "files": {f"{video_id}.en-orig.vtt": vtt}}


def _listing(*rows):
    return {"stdout": "".join(f"{vid}\t{day}\ttitle {vid}\n" for vid, day in rows)}


RECORDINGS = {
    youtube.channel_url("UCalpha"): _listing(("a1", "20261107"), ("a2", "20261106"), ("old_a", "20261101")),
    youtube.channel_url("UCbeta"): _listing(("b1", "20261107"), ("b2", "20261106"), ("old_b", "20261030")),
    # g0 is listed on the cutoff day but was really posted the day before, so it is left out.
    youtube.channel_url("UCgamma"): _listing(("g1", "20261106"), ("g0", "20261105"), ("old_g", "20261020")),
    _url("g0"): {"stdout": "20261104\t1762300000\n"},
    **{_url(vid): _captions(vid) for vid in TRANSCRIPTS},
}
RATE_LIMITED_B1 = {**RECORDINGS, _url("b1"): {"error": RATE_LIMITED, "message": "HTTP Error 429"}}


def _row(video_id, ticker, company, sentiment, quote, *, conviction=4, thesis=None, catalyst=None,
         price_target=None, asset_type="stock"):
    return {"video_id": video_id, "channel": TRANSCRIPTS[video_id][0], "ticker": ticker, "company": company,
            "asset_type": asset_type, "mentions": 2, "sentiment": sentiment, "conviction": conviction,
            "is_primary_topic": True, "recommendation": "buy" if sentiment == "bullish" else "none",
            "thesis": thesis, "catalyst": catalyst, "time_horizon": "long", "price_target": price_target,
            "quote": quote}


AMZN = _row("b2", "AMZN", "Amazon", "bullish", "I also bought more Amazon for the AWS business",
            thesis="Bought more for the growth of the AWS cloud business.")

EXTRACT = {"videos": [
    {"video_id": "a1", "status": "done", "rows": [
        _row("a1", "NVDA", "Nvidia", "bullish", "I am loading up on NVDA before earnings", conviction=5,
             thesis="Data center demand keeps growing, so he is loading up before earnings.", catalyst="earnings")]},
    {"video_id": "a2", "status": "done", "rows": [
        _row("a2", "TSLA", "Tesla", "bullish", "Tesla looks weak to me", conviction=3,
             thesis="Deliveries are falling, and he is trading out of his shares.")]},
    {"video_id": "b1", "status": "done", "rows": [
        _row("b1", "MSFT", "Microsoft", "bullish", "Microsoft is a buy for me",
             thesis="Prefers Microsoft over Apple for the growth of its cloud business."),
        _row("b1", "AAPL", "Apple", "neutral", "Apple is fine", conviction=1)]},
    {"video_id": "b2", "status": "done", "rows": [
        _row("b2", "PLTR", "Palantir", "bullish", "I think it can double from here",
             thesis="Keeps winning government contracts and can double from here.", price_target="2x")]},
    {"video_id": "g1", "status": "done", "rows": [
        _row("g1", "BTC", "Bitcoin", "bearish", "Bitcoin could fall to fifty thousand", asset_type="crypto",
             thesis="The weekly trend broke, so it could fall to fifty thousand.", price_target="$50,000"),
        _row("g1", "ETH", "Ethereum", "bearish", "Ethereum is also heading lower", conviction=2,
             asset_type="crypto", thesis="Expects it to keep heading lower along with bitcoin.")]},
]}

R1 = {"findings": [
    {"id": "f1", "video_id": "a2", "ticker": "TSLA", "issue": "wrong_sentiment",
     "proposed": {"sentiment": "bearish"}, "quote": "I am selling my shares"},
    {"id": "f2", "video_id": "b1", "ticker": "AAPL", "issue": "not_discussed", "proposed": {},
     "quote": "I prefer Microsoft for the cloud growth"},
    {"id": "f3", "video_id": "g1", "ticker": "ETH", "issue": "wrong_ticker",
     "proposed": {"ticker": "ETHE", "company": "Grayscale Ethereum Trust", "asset_type": "etf"},
     "quote": "Ethereum is also heading lower"},
    {"id": "f4", "video_id": "b2", "ticker": None, "issue": "missed_ticker", "proposed": AMZN,
     "quote": "I also bought more Amazon for the AWS business"},
]}
RESPONSE_R1 = {"responses": [
    {"id": "f1", "decision": "accept", "quote": None, "reason": "He is selling."},
    {"id": "f2", "decision": "reject", "quote": "Apple is fine", "reason": "Apple is named."},
    {"id": "f3", "decision": "reject", "quote": "Ethereum is also heading lower", "reason": "The coin, not a fund."},
    {"id": "f4", "decision": "accept", "quote": None, "reason": "Missed it."},
]}
R2 = {"verdicts": [{"id": "f2", "verdict": "withdraw"}, {"id": "f3", "verdict": "hold"}], "findings": []}

ANSWERS = {"extract_01": EXTRACT, "review_01_r1": R1, "response_01_r1": RESPONSE_R1, "review_01_r2": R2}
ALL_STAGES = ["channels", "transcripts", "corpus", "extract", "review", "grade", "report"]


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    """Config under tmp_path, a passing doctor, and the recorded yt-dlp output installed."""
    out = tmp_path / "out"
    path = tmp_path / "config.toml"
    channels = ", ".join(f'["{name}", "{cid}"]' for name, cid in CHANNELS)
    path.write_text(f"output_dir = '{out}'\npace_seconds = 0\nllm_workers = 1\nchannels = [{channels}]\n",
                    encoding="utf-8")
    monkeypatch.setattr(stockyt_config, "DEFAULT_CONFIG_PATH", str(path))
    monkeypatch.setattr(pipeline, "doctor", lambda settings, backend: [(True, "doctor stubbed")])
    monkeypatch.setattr(pipeline, "FAKE_ANSWERS", dict(ANSWERS))
    return out


@pytest.fixture
def ytdlp(monkeypatch):
    """Install recordings; a call with --cookies-from-browser gets `with_cookies`. Records every URL asked."""
    state = {"calls": []}

    def install(recordings, with_cookies=RECORDINGS):
        plain_run, stream = youtube.replay(recordings)
        cookie_run, _ = youtube.replay(with_cookies)

        def run(args, timeout, cwd=None):
            state["calls"].append(args[-1])
            return (cookie_run if "--cookies-from-browser" in args else plain_run)(args, timeout, cwd)

        def lines(args):
            state["calls"].append(args[-1])
            return stream(args)

        monkeypatch.setattr(youtube, "run_ytdlp", run)
        monkeypatch.setattr(youtube, "stream_ytdlp", lines)

    state["install"] = install
    return state


def _run_json(out, label="e2e"):
    return json.loads((out / "runs" / label / "run.json").read_text(encoding="utf-8"))


def _statuses(out, label="e2e"):
    return {name: rec["status"] for name, rec in _run_json(out, label)["stages"].items()}


def _llm_calls(out, label="e2e"):
    lines = (out / "runs" / label / "log.jsonl").read_text(encoding="utf-8").splitlines()
    return [line["id"] for line in map(json.loads, lines) if line["kind"] == "llm"]


def _start(label="e2e", *extra):
    return main(["run", "--since", SINCE, "--label", label, "--llm", "fake", *extra])


def test_rate_limit_blocks_then_resume_with_cookies_finishes_with_the_golden_report(out_dir, ytdlp, capsys):
    ytdlp["install"](RATE_LIMITED_B1)
    assert _start() == 2
    out = capsys.readouterr().out
    assert "agent-reach stockyt run --resume e2e --cookies-from-browser chrome" in out
    assert "Transcripts: 5 links · 2 ok · 1 error (1 rate_limited) · 2 missing [FAIL]" in out
    assert _statuses(out_dir) == {"channels": "done", "transcripts": "blocked"}

    ytdlp["calls"].clear()
    assert main(["run", "--resume", "e2e", "--cookies-from-browser", "chrome"]) == 0
    out = capsys.readouterr().out
    assert "[channels] unchanged, skipped" in out
    assert not any("/channel/" in url for url in ytdlp["calls"])  # the listing is not fetched again
    assert "Extract: 5/5 videos [OK]" in out and "Review: OK" in out
    assert _statuses(out_dir) == dict.fromkeys(ALL_STAGES, "done")
    run_json = _run_json(out_dir)
    assert run_json["params"]["cookies_from_browser"] == "chrome"
    assert run_json["config_snapshot"]["channels"] == [list(c) for c in CHANNELS]

    run_dir = out_dir / "runs" / "e2e"
    assert (run_dir / "report.md").read_text(encoding="utf-8") == GOLDEN.read_text(encoding="utf-8")
    # The R2 review had no findings, so no R2 response was asked; code wrote the empty file.
    assert _llm_calls(out_dir) == ["extract_01", "review_01_r1", "response_01_r1", "review_01_r2"]
    assert json.loads((run_dir / "chunks" / "response_01_r2.json").read_text(encoding="utf-8")) == []
    tickers = json.loads((run_dir / "tickers.json").read_text(encoding="utf-8"))
    assert [(c["finding"], c["status"]) for c in tickers["review_changes"]] == [
        ("f1", "corrected"), ("f3", "disputed"), ("f4", "added")]
    assert tickers["integrity"] == {"n": 5, "extracted": 5, "missing": []}


def test_a_crash_after_transcripts_resumes_without_fetching_again(out_dir, ytdlp, monkeypatch, capsys):
    ytdlp["install"](RECORDINGS)

    def crash(task):
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "FAKE_ANSWERS", {"extract_01": crash})
    with pytest.raises(KeyboardInterrupt):
        _start()
    assert _statuses(out_dir) == {"channels": "done", "transcripts": "done", "corpus": "done",
                                  "extract": "pending"}
    assert not (out_dir / "runs" / "e2e" / ".lock").exists()

    monkeypatch.setattr(pipeline, "FAKE_ANSWERS", dict(ANSWERS))
    ytdlp["calls"].clear()
    assert main(["run", "--resume", "e2e"]) == 0
    assert ytdlp["calls"] == []
    assert "[transcripts] unchanged, skipped" in capsys.readouterr().out
    report = (out_dir / "runs" / "e2e" / "report.md").read_text(encoding="utf-8")
    assert report == GOLDEN.read_text(encoding="utf-8")


def test_session_backend_waits_with_exit_3_until_every_prompt_is_answered(out_dir, ytdlp, capsys):
    ytdlp["install"](RECORDINGS)
    session = out_dir / "runs" / "e2e" / "llm" / "session"
    assert main(["run", "--since", SINCE, "--label", "e2e", "--llm", "session"]) == 3
    assert _statuses(out_dir)["extract"] == "waiting"
    assert str(session / "extract_01.prompt.md") in capsys.readouterr().out

    answered = []
    for _ in range(5):
        prompts = pipeline.pending_prompts(pipeline.run_mod.open_run(str(out_dir), "e2e"))
        if not prompts:
            break
        for prompt in prompts:
            task_id = Path(prompt).name.removesuffix(".prompt.md")
            (session / f"{task_id}.answer.json").write_text(json.dumps(ANSWERS[task_id]), encoding="utf-8")
            answered.append(task_id)
        code = main(["run", "--resume", "e2e"])
        if code != 3:
            break
    assert code == 0
    assert answered == ["extract_01", "review_01_r1", "response_01_r1", "review_01_r2"]
    report = (out_dir / "runs" / "e2e" / "report.md").read_text(encoding="utf-8")
    assert report == GOLDEN.read_text(encoding="utf-8")


def test_an_answer_still_invalid_after_the_reask_exits_1_until_retry_failed(out_dir, ytdlp, monkeypatch, capsys):
    ytdlp["install"](RECORDINGS)
    bad = json.loads(json.dumps(EXTRACT))
    bad["videos"][0]["rows"][0]["quote"] = "words nobody said"
    monkeypatch.setattr(pipeline, "FAKE_ANSWERS", {"extract_01": bad, "extract_01-reask": bad})
    assert _start() == 1
    out = capsys.readouterr().out
    assert "Extract: 4/5 videos [FAIL]" not in out  # a failed chunk counts none of its videos
    assert "Extract: 0/5 videos [FAIL]" in out
    assert "❌ extract_01: unrecovered after one re-ask" in out and "words nobody said" in out
    assert "agent-reach stockyt run --resume e2e --retry-failed" in out
    assert _statuses(out_dir)["extract"] == "failed"

    monkeypatch.setattr(pipeline, "FAKE_ANSWERS", dict(ANSWERS))
    assert main(["run", "--resume", "e2e"]) == 1  # unrecovered stays unrecovered
    assert _llm_calls(out_dir) == ["extract_01", "extract_01-reask"]

    assert main(["run", "--resume", "e2e", "--retry-failed"]) == 0
    assert _statuses(out_dir) == dict.fromkeys(ALL_STAGES, "done")


def test_an_invalid_review_answer_fails_the_review_stage(out_dir, ytdlp, monkeypatch, capsys):
    ytdlp["install"](RECORDINGS)
    bad = {"findings": [{**R1["findings"][0], "quote": "made up"}]}
    monkeypatch.setattr(pipeline, "FAKE_ANSWERS", {**ANSWERS, "review_01_r1": bad, "review_01_r1-reask": bad})
    assert _start() == 1
    out = capsys.readouterr().out
    assert "❌ review chunk 01: unrecovered after one re-ask" in out
    assert _statuses(out_dir)["review"] == "failed"
    assert not (out_dir / "runs" / "e2e" / "chunks" / "review_summary.json").exists()


def test_a_listing_that_ends_before_the_window_start_fails_the_channels_check(out_dir, ytdlp, capsys):
    short = {**RECORDINGS, youtube.channel_url("UCgamma"): _listing(("g1", "20261106"))}
    ytdlp["install"](short)
    assert _start() == 1
    out = capsys.readouterr().out
    assert "❌ Gamma: listing ended before the window start" in out
    assert _statuses(out_dir) == {"channels": "failed"}


def test_a_window_with_no_videos_fails_the_corpus_check(out_dir, ytdlp, capsys):
    ytdlp["install"](RECORDINGS)
    assert main(["run", "--since", "20261108", "--label", "empty", "--llm", "fake"]) == 1
    assert "Corpus: 0 videos · 0 channels · 0 chunks [FAIL]" in capsys.readouterr().out
    assert _statuses(out_dir, "empty")["corpus"] == "failed"


def test_regrade_rewrites_the_report_with_the_current_weights(out_dir, ytdlp, tmp_path, capsys):
    ytdlp["install"](RECORDINGS)
    assert _start() == 0
    config = tmp_path / "config.toml"
    config.write_text(config.read_text(encoding="utf-8") + "[weights]\ncoverage_smoothing_k = 0\n", encoding="utf-8")
    capsys.readouterr()

    assert main(["regrade", "e2e"]) == 0
    out = capsys.readouterr().out
    assert "[grade]" in out and "[report]" in out and "[extract]" not in out
    assert _run_json(out_dir)["config_snapshot"]["weights"]["coverage_smoothing_k"] == 0
    tickers = json.loads((out_dir / "runs" / "e2e" / "tickers.json").read_text(encoding="utf-8"))
    assert tickers["bullish"][0]["ticker"] == "NVDA"



def test_a_run_from_before_markets_resumes_with_no_stage_run_again(out_dir, ytdlp, capsys):
    ytdlp["install"](RECORDINGS)
    assert _start() == 0
    run_dir = out_dir / "runs" / "e2e"
    stages = _run_json(out_dir)["stages"]
    # The input hashes as stockyt computed them before runs had a market.
    assert stages["channels"]["input_hash"] == "0f0fac2c9fd88d0f508cd3b8ce014134b20a08ead3a4248d2812e928dfef5484"
    assert stages["transcripts"]["input_hash"] == pipeline.input_hash(
        {"links": pipeline._digest([str(run_dir / "links.txt")]), "lang": "en", "whisper": False})

    # Such a run's settings copy has `lang` but no `market` or `caption_langs`.
    run_json = _run_json(out_dir)
    for key in ("market", "caption_langs"):
        run_json["config_snapshot"].pop(key, None)
    (run_dir / "run.json").write_text(json.dumps(run_json), encoding="utf-8")
    ytdlp["calls"].clear()
    capsys.readouterr()

    assert main(["run", "--resume", "e2e"]) == 0
    out = capsys.readouterr().out
    assert all(f"[{name}] unchanged, skipped" in out for name in ALL_STAGES)
    assert ytdlp["calls"] == []
    assert _llm_calls(out_dir) == ["extract_01", "review_01_r1", "response_01_r1", "review_01_r2"]



# ── India: two channels, one speaking Hindi (only a `hi-orig` track), one English ──

INDIA_CHANNELS = [("Hindi Desk", "UChindi"), ("English Desk", "UCeng")]
INDIA_TEXT = {"h1": ("Hindi Desk", "hi-orig", "रिलायंस में खरीदारी करें क्योंकि जियो का कारोबार बढ़ रहा है"),
              "e1": ("English Desk", "en-orig", "HDFC Bank looks strong after the results, I am buying it.")}


def _india_captions(video_id):
    channel, lang, text = INDIA_TEXT[video_id]
    return {"stdout": f"{channel}\t{dict(INDIA_CHANNELS)[channel]}\t20261108\t1762600000\n",
            "files": {f"{video_id}.{lang}.vtt": f"WEBVTT\n\n00:00:00.000 --> 00:00:09.000\n{text}\n"}}


INDIA_RECORDINGS = {
    youtube.channel_url("UChindi"): _listing(("h1", "20261108"), ("old_h", "20261101")),
    youtube.channel_url("UCeng"): _listing(("e1", "20261108"), ("old_e", "20261101")),
    **{_url(vid): _india_captions(vid) for vid in INDIA_TEXT},
}


def _india_row(video_id, ticker, company, quote, thesis):
    return {**_row("a1", ticker, company, "bullish", quote, thesis=thesis), "video_id": video_id,
            "channel": INDIA_TEXT[video_id][0]}


INDIA_EXTRACT = {"videos": [
    {"video_id": "h1", "status": "done", "rows": [
        _india_row("h1", "RELIANCE", "Reliance Industries", "रिलायंस में खरीदारी करें",
                   "Buy Reliance because the Jio business keeps growing.")]},
    {"video_id": "e1", "status": "done", "rows": [
        _india_row("e1", "HDFCBANK", "HDFC Bank", "I am buying it", "Looks strong after the results.")]},
]}


def test_an_india_run_reads_hindi_captions_and_resumes_only_as_india(out_dir, ytdlp, monkeypatch, capsys):
    monkeypatch.setattr(stockyt_config, "INDIA_CHANNELS", INDIA_CHANNELS)
    prompts = []

    def extract(task):
        prompts.append(task.prompt)
        return INDIA_EXTRACT

    monkeypatch.setattr(pipeline, "FAKE_ANSWERS", {"extract_01": extract, "review_01_r1": {"findings": []}})
    ytdlp["install"](INDIA_RECORDINGS)
    assert main(["run", "--since", SINCE, "--label", "india", "--llm", "fake", "--market", "india"]) == 0

    run_dir = out_dir / "runs" / "india"
    run_json = _run_json(out_dir, "india")
    assert run_json["config_snapshot"]["market"] == "india"
    assert run_json["config_snapshot"]["timezone"] == "Asia/Kolkata"
    rows = [json.loads(line) for line in (run_dir / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["video_id"], r["lang"], r["channel"]) for r in rows] == [
        ("h1", "hi-orig", "Hindi Desk"), ("e1", "en-orig", "English Desk")]
    assert "NSE symbol" in prompts[0] and "रिलायंस में खरीदारी करें" in prompts[0]
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert report.startswith("# Ticker Leaderboard — India (2 tickers · 2 authors)")
    assert "| RELIANCE | Reliance Industries |" in report
    capsys.readouterr()

    assert main(["run", "--resume", "india"]) == 0
    out = capsys.readouterr().out
    assert all(f"[{name}] unchanged, skipped" in out for name in ALL_STAGES)
    assert main(["run", "--resume", "india", "--market", "us"]) == 1
    assert "run 'india' is for the India market: drop --market" in capsys.readouterr().err
    assert main(["status", "india"]) == 0
    assert "· market India" in capsys.readouterr().out
