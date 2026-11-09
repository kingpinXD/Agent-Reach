# -*- coding: utf-8 -*-
"""Deterministic ticker grader.

Turns extracted per-mention rows into two ranked leaderboards (bullish /
bearish) using cross-author consensus. No LLM — pure arithmetic over the
extract rows. Ported from the validated throwaway grader; weights are tunable
module constants.
"""

from __future__ import annotations

import glob
import json
import os
from collections import Counter, defaultdict

# ── weights (tunable) ──
W_THESIS = 0.45
W_CATALYST = 0.20
W_PRICE_TARGET = 0.15
W_PRIMARY = 0.10
W_HORIZON = 0.10
# Coverage on one side is m / (n + K): m = the channel's videos taking that side, n = all its
# videos. K keeps a one-video channel from scoring full coverage on everything it mentions.
COVERAGE_K = 1

# Tickers that map onto a canonical symbol before grading.
ALIASES = {"GOOG": "GOOGL", "BRK.B": "BRK.A", "SPCX": "SPACEX", "SSPC": "SPACEX"}

SENTIMENT = {
    "bullish": 1,
    "bull": 1,
    "positive": 1,
    "bearish": -1,
    "bear": -1,
    "negative": -1,
    "neutral": 0,
}

_EMPTY = ("", "null", "none", "n/a", "na")


def _substantive(value) -> bool:
    """A thesis counts only if it's real prose, not a null-ish stub."""
    if not value:
        return False
    s = str(value).strip().lower()
    return s not in _EMPTY and len(s) > 10


def _present(value) -> bool:
    """A field counts as present if it isn't empty/null/none."""
    if not value:
        return False
    return str(value).strip().lower() not in ("", "null", "none")


def _context_score(row: dict) -> float:
    """Per-mention context score, clamped to 1.0."""
    score = 0.0
    if _substantive(row.get("thesis")):
        score += W_THESIS
    if _present(row.get("catalyst")):
        score += W_CATALYST
    if _present(row.get("price_target")):
        score += W_PRICE_TARGET
    if row.get("is_primary_topic") in (True, "true", 1):
        score += W_PRIMARY
    if row.get("time_horizon") and row["time_horizon"] != "unspecified":
        score += W_HORIZON
    return min(score, 1.0)


def _normalize_ticker(row: dict) -> str | None:
    ticker = row.get("ticker")
    if not ticker:
        return None
    ticker = str(ticker).upper()
    return ALIASES.get(ticker, ticker)


def _tier(score: float) -> str:
    if score >= 80:
        return "S"
    if score >= 60:
        return "A"
    if score >= 40:
        return "B"
    return "C"


def _side_strength(rows: list[dict], side: int, n_videos: int, k: float) -> tuple[float, int]:
    """(strength, videos) of one channel's rows on one side (+1 bullish, -1 bearish) of a ticker."""
    side_rows = [r for r in rows if _sentiment(r) == side]
    if not side_rows:
        return 0.0, 0
    videos = len({r["video_id"] for r in side_rows})
    coverage = videos / (n_videos + k)
    ctx = sum(_context_score(r) for r in side_rows) / len(side_rows)
    conv = _conviction(side_rows)
    return coverage * (0.25 + 0.75 * ctx) * (0.5 + 0.5 * conv), videos


def _sentiment(row: dict) -> int:
    return SENTIMENT.get(str(row.get("sentiment", "neutral")).lower(), 0)


def _conviction(rows: list[dict]) -> float:
    """Mean conviction scaled to 0-1."""
    return sum(float(r.get("conviction") or 1) for r in rows) / len(rows) / 5.0


def _author_stats(mentions: list[dict], videos_per_author: dict, k: float) -> dict:
    """Per-author strength on each side, plus overall sentiment/context/conviction, for one ticker."""
    per_author: dict[str, list[dict]] = defaultdict(list)
    for row in mentions:
        per_author[row.get("channel")].append(row)

    stats: dict[str, dict] = {}
    for author, rows in per_author.items():
        if author not in videos_per_author:
            continue
        n_videos = videos_per_author[author]
        bull, bull_videos = _side_strength(rows, 1, n_videos, k)
        bear, bear_videos = _side_strength(rows, -1, n_videos, k)
        stats[author] = {
            "bull": bull,
            "bear": bear,
            "bull_videos": bull_videos,
            "bear_videos": bear_videos,
            "sent": sum(_sentiment(r) for r in rows) / len(rows),
            "ctx": sum(_context_score(r) for r in rows) / len(rows),
            "conv": _conviction(rows),
        }
    return stats


def _top_theses(mentions: list[dict], side: int) -> list[tuple]:
    """(channel, thesis) of one side's substantive theses, strongest (ctx x conviction) first."""
    rows = [r for r in mentions if _sentiment(r) == side and _substantive(r.get("thesis"))]
    rows.sort(key=lambda r: -_context_score(r) * float(r.get("conviction") or 1))
    return [(r.get("channel"), r.get("thesis")) for r in rows[:3]]


def _ticker_summary(ticker: str, mentions: list[dict], stats: dict) -> dict:
    bull = [a for a, s in stats.items() if s["bull_videos"]]
    bear = [a for a, s in stats.items() if s["bear_videos"]]
    bull_score = len(bull) * sum(stats[a]["bull"] for a in bull)
    bear_score = len(bear) * sum(stats[a]["bear"] for a in bear)
    net = sum(s["sent"] for s in stats.values()) / len(stats)

    catalysts = [r.get("catalyst") for r in mentions if _present(r.get("catalyst"))]
    price_targets = [r.get("price_target") for r in mentions if _present(r.get("price_target"))]

    return {
        "ticker": ticker,
        "company": Counter(r.get("company") for r in mentions).most_common(1)[0][0],
        "asset_type": Counter(r.get("asset_type") for r in mentions).most_common(1)[0][0],
        "breadth": len(stats),
        "bull_breadth": len(bull),
        "bear_breadth": len(bear),
        "authors": sorted(stats.keys()),
        "n_videos": len({r["video_id"] for r in mentions}),
        "net_sentiment": round(net, 2),
        "avg_conviction": round(sum(s["conv"] for s in stats.values()) / len(stats) * 5, 1),
        "avg_context": round(sum(s["ctx"] for s in stats.values()) / len(stats), 2),
        "bull_score": bull_score,
        "bear_score": bear_score,
        "contested": any(s["sent"] > 0 for s in stats.values())
        and any(s["sent"] < 0 for s in stats.values()),
        "top_theses": {"bullish": _top_theses(mentions, 1), "bearish": _top_theses(mentions, -1)},
        "catalysts": list(dict.fromkeys(catalysts))[:3],
        "price_targets": list(dict.fromkeys(price_targets))[:3],
    }


def _board(summaries: list[dict], score_key: str) -> list[dict]:
    """Rank by score_key desc, normalize to 0-100 vs the max, attach tier+score."""
    ranked = sorted((s for s in summaries if s[score_key] > 0), key=lambda s: -s[score_key])
    top = ranked[0][score_key] if ranked else 1
    board = []
    for summary in ranked:
        normalized = round(100 * summary[score_key] / top, 1)
        board.append({**summary, "score": normalized, "tier": _tier(normalized)})
    return board


def grade(extract_rows: list[dict], videos_per_author: dict, total_authors: int,
          k: float = COVERAGE_K) -> dict:
    """Grade extracted ticker mentions into bullish/bearish leaderboards.

    Returns {"bullish": [...], "bearish": [...], "tickers": {ticker: summary},
    "disputed": {ticker: count}}. Rows the review dropped or left disputed add
    nothing to any ticker; disputed ones are only counted in `disputed`.
    `total_authors` is carried through for downstream breadth display. `k` is the coverage
    smoothing (see COVERAGE_K).
    """
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    disputed: Counter = Counter()
    for row in extract_rows:
        ticker = _normalize_ticker(row)
        if ticker is None:
            continue
        status = row.get("review_status")
        if status == "disputed":
            disputed[ticker] += 1
        if status in ("dropped", "disputed"):
            continue
        by_ticker[ticker].append(row)

    summaries: dict[str, dict] = {}
    for ticker, mentions in by_ticker.items():
        stats = _author_stats(mentions, videos_per_author, k)
        if not stats:
            continue
        summaries[ticker] = _ticker_summary(ticker, mentions, stats)

    all_summaries = list(summaries.values())
    return {
        "total_authors": total_authors,
        "bullish": _board(all_summaries, "bull_score"),
        "bearish": _board(all_summaries, "bear_score"),
        "tickers": summaries,
        "disputed": dict(disputed),
    }


def _thesis_cell(entry, width: int = 70) -> str:
    """First top_theses entry's text, truncated, with '|' neutralized for tables."""
    if not entry:
        return ""
    text = str(entry[1] if isinstance(entry, (list, tuple)) else entry).strip()
    text = text.replace("|", "/")
    return text[: width - 1] + "…" if len(text) > width else text


def _md_table(rows: list[dict], side: str, total_authors: int, disputed: dict | None = None,
              limit: int = 25) -> str:
    """One board's table (`side` is bullish or bearish): its own theses, and Breadth as that
    side's channel count next to every channel mentioning the ticker. A Disputed column is
    added when `disputed` is given (reviewed runs)."""
    short = side[:4]
    extra = " Disputed |" if disputed is not None else ""
    header = (
        f"| Rank | Ticker | Company | Tier | Score | Breadth | Contested |{extra} Thesis |\n"
        "|---|---|---|---|---|---|---|" + ("---|" if disputed is not None else "") + "---|"
    )
    lines = [header]
    for rank, v in enumerate(rows[:limit], 1):
        thesis = _thesis_cell((v["top_theses"][side] or [None])[0])
        contested = "⚔️" if v.get("contested") else ""
        cell = f" {disputed.get(v['ticker'], 0) or ''} |" if disputed is not None else ""
        lines.append(
            f"| {rank} | {v['ticker']} | {str(v.get('company') or '').replace('|', '/')} "
            f"| {v['tier']} | {v['score']} | {v[short + '_breadth']} {short} · {v['breadth']}/{total_authors} "
            f"| {contested} |{cell} {thesis} |"
        )
    return "\n".join(lines)


def review_line(review: dict, disputed: dict) -> str:
    """One-line summary of what the two-round review changed."""
    counts = " · ".join(f"{review[s]} {s}" for s in ("confirmed", "corrected", "added", "dropped", "disputed"))
    line = f"Review: {counts} (+{review['disputed_missed']} disputed missed tickers, not added)"
    if disputed:
        top = sorted(disputed.items(), key=lambda kv: (-kv[1], kv[0]))
        line += ". Disputed rows (left out of the scores): " + ", ".join(f"{t} {n}" for t, n in top)
    return line


def leaderboard_markdown(result: dict) -> str:
    """Render the graded result as a Markdown leaderboard (top 25 each board).

    A reviewed result (one carrying `review` counts) also gets a review line and a Disputed column.
    """
    total_authors = result["total_authors"]
    total_tickers = len(result["tickers"])
    review = result.get("review")
    disputed = result.get("disputed", {}) if review else None
    parts = [f"# Ticker Leaderboard ({total_tickers} tickers · {total_authors} authors)", ""]
    if review:
        parts += [review_line(review, disputed), ""]
    parts += [
        "## Bullish",
        "",
        _md_table(result["bullish"], "bullish", total_authors, disputed),
        "",
        "## Bearish",
        "",
        _md_table(result["bearish"], "bearish", total_authors, disputed),
        "",
    ]
    return "\n".join(parts)


def _union_video_ids(paths: list[str]) -> set[str]:
    """Union of video_ids across a set of JSONL files (malformed lines skipped)."""
    ids: set[str] = set()
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                vid = row.get("video_id")
                if vid:
                    ids.add(vid)
    return ids


def integrity(extract_dir: str, n: int) -> dict | None:
    """Reconcile corpus chunks vs extracts in extract_dir.

    Returns None (skip) when no chunk_*.jsonl files are present. Otherwise:
    covered = union of video_ids across chunk_*.jsonl, with_tickers = union
    across extract_*.jsonl, missing = N - len(covered) (corpus videos that
    never made it into any chunk). ok when missing == 0.
    """
    chunk_paths = sorted(glob.glob(os.path.join(extract_dir, "chunk_*.jsonl")))
    if not chunk_paths:
        return None
    extract_paths = sorted(glob.glob(os.path.join(extract_dir, "extract_*.jsonl")))
    covered = _union_video_ids(chunk_paths)
    with_tickers = _union_video_ids(extract_paths)
    missing = max(n - len(covered), 0)
    return {
        "n": n,
        "covered": len(covered),
        "with_tickers": len(with_tickers),
        "missing": missing,
        "ok": missing == 0,
    }
