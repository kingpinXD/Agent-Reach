# -*- coding: utf-8 -*-
"""Deterministic ticker grader.

Turns extracted per-mention rows into two ranked leaderboards (bullish /
bearish) using cross-author consensus. No LLM — pure arithmetic over the
extract rows. Ported from the validated throwaway grader; weights are tunable
module constants.
"""

from __future__ import annotations

from collections import Counter, defaultdict

# ── weights (tunable) ──
W_THESIS = 0.45
W_CATALYST = 0.20
W_PRICE_TARGET = 0.15
W_PRIMARY = 0.10
W_HORIZON = 0.10

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


def _author_stats(mentions: list[dict], videos_per_author: dict) -> dict:
    """Per-author strength/sentiment/context for one ticker."""
    per_author: dict[str, list[dict]] = defaultdict(list)
    for row in mentions:
        per_author[row.get("channel")].append(row)

    stats: dict[str, dict] = {}
    for author, rows in per_author.items():
        if author not in videos_per_author:
            continue
        distinct_videos = {r["video_id"] for r in rows}
        coverage = len(distinct_videos) / videos_per_author[author]
        ctx = sum(_context_score(r) for r in rows) / len(rows)
        conv = sum(float(r.get("conviction") or 1) for r in rows) / len(rows) / 5.0
        sent = sum(SENTIMENT.get(str(r.get("sentiment", "neutral")).lower(), 0) for r in rows) / len(rows)
        strength = coverage * (0.25 + 0.75 * ctx) * (0.5 + 0.5 * conv)
        stats[author] = {"strength": strength, "sent": sent, "ctx": ctx, "conv": conv}
    return stats


def _ticker_summary(ticker: str, mentions: list[dict], stats: dict) -> dict:
    bull = [a for a, s in stats.items() if s["sent"] > 0]
    bear = [a for a, s in stats.items() if s["sent"] < 0]
    bull_score = len(bull) * sum(stats[a]["strength"] for a in bull)
    bear_score = len(bear) * sum(stats[a]["strength"] for a in bear)
    net = sum(s["sent"] for s in stats.values()) / len(stats)

    theses = [(r.get("channel"), r.get("thesis")) for r in mentions if _substantive(r.get("thesis"))]
    catalysts = [r.get("catalyst") for r in mentions if _present(r.get("catalyst"))]
    price_targets = [r.get("price_target") for r in mentions if _present(r.get("price_target"))]

    return {
        "ticker": ticker,
        "company": Counter(r.get("company") for r in mentions).most_common(1)[0][0],
        "asset_type": Counter(r.get("asset_type") for r in mentions).most_common(1)[0][0],
        "breadth": len(stats),
        "authors": sorted(stats.keys()),
        "n_videos": len({r["video_id"] for r in mentions}),
        "net_sentiment": round(net, 2),
        "avg_conviction": round(sum(s["conv"] for s in stats.values()) / len(stats) * 5, 1),
        "avg_context": round(sum(s["ctx"] for s in stats.values()) / len(stats), 2),
        "bull_score": bull_score,
        "bear_score": bear_score,
        "contested": len(bull) >= 1 and len(bear) >= 1,
        "top_theses": theses[:3],
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


def grade(extract_rows: list[dict], videos_per_author: dict, total_authors: int) -> dict:
    """Grade extracted ticker mentions into bullish/bearish leaderboards.

    Returns {"bullish": [...], "bearish": [...], "tickers": {ticker: summary}}.
    `total_authors` is carried through for downstream breadth display.
    """
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for row in extract_rows:
        ticker = _normalize_ticker(row)
        if ticker is None:
            continue
        by_ticker[ticker].append(row)

    summaries: dict[str, dict] = {}
    for ticker, mentions in by_ticker.items():
        stats = _author_stats(mentions, videos_per_author)
        if not stats:
            continue
        summaries[ticker] = _ticker_summary(ticker, mentions, stats)

    all_summaries = list(summaries.values())
    return {
        "total_authors": total_authors,
        "bullish": _board(all_summaries, "bull_score"),
        "bearish": _board(all_summaries, "bear_score"),
        "tickers": summaries,
    }
