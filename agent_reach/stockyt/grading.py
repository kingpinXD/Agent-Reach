# -*- coding: utf-8 -*-
"""Deterministic ticker grader.

Turns extracted per-mention rows into two ranked leaderboards (bullish /
bearish) using cross-author consensus. No LLM — pure arithmetic over the
extract rows. The weights come from the settings (`config.Weights`).

Coverage on one side is m / (n + K): m = the channel's videos taking that side, n = all its
videos. K (`Weights.coverage_smoothing_k`) keeps a one-video channel from scoring full coverage
on everything it mentions.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from agent_reach.stockyt.config import Weights

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


def _context_score(row: dict, w: Weights) -> float:
    """Per-mention context score, clamped to 1.0."""
    score = 0.0
    if _substantive(row.get("thesis")):
        score += w.thesis
    if _present(row.get("catalyst")):
        score += w.catalyst
    if _present(row.get("price_target")):
        score += w.price_target
    if row.get("is_primary_topic") in (True, "true", 1):
        score += w.primary
    if row.get("time_horizon") and row["time_horizon"] != "unspecified":
        score += w.horizon
    return min(score, 1.0)


def _normalize_ticker(row: dict) -> str | None:
    """The row's ticker, or its company name when the extractor left the ticker null.

    The company name is uppercased with non-alphanumerics removed ("SpaceX" -> SPACEX), so a
    private company graded by name lands on the same key as an aliased ticker for it.
    """
    ticker = str(row.get("ticker") or "").upper()
    if not ticker:
        ticker = re.sub(r"[^A-Z0-9]", "", str(row.get("company") or "").upper())
    if not ticker:
        return None
    return ALIASES.get(ticker, ticker)


def _tier(score: float) -> str:
    if score >= 80:
        return "S"
    if score >= 60:
        return "A"
    if score >= 40:
        return "B"
    return "C"


def _side_strength(rows: list[dict], side: int, n_videos: int, w: Weights) -> tuple[float, int]:
    """(strength, videos) of one channel's rows on one side (+1 bullish, -1 bearish) of a ticker."""
    side_rows = [r for r in rows if _sentiment(r) == side]
    if not side_rows:
        return 0.0, 0
    videos = len({r["video_id"] for r in side_rows})
    coverage = videos / (n_videos + w.coverage_smoothing_k)
    ctx = sum(_context_score(r, w) for r in side_rows) / len(side_rows)
    conv = _conviction(side_rows)
    return coverage * (0.25 + 0.75 * ctx) * (0.5 + 0.5 * conv), videos


def _sentiment(row: dict) -> int:
    return SENTIMENT.get(str(row.get("sentiment", "neutral")).lower(), 0)


def _conviction(rows: list[dict]) -> float:
    """Mean conviction scaled to 0-1."""
    return sum(float(r.get("conviction") or 1) for r in rows) / len(rows) / 5.0


def _author_stats(mentions: list[dict], videos_per_author: dict, w: Weights) -> dict:
    """Per-author strength on each side, plus overall sentiment/context/conviction, for one ticker."""
    per_author: dict[str, list[dict]] = defaultdict(list)
    for row in mentions:
        per_author[row.get("channel") or ""].append(row)

    stats: dict[str, dict] = {}
    for author, rows in per_author.items():
        if author not in videos_per_author:
            continue
        n_videos = videos_per_author[author]
        bull, bull_videos = _side_strength(rows, 1, n_videos, w)
        bear, bear_videos = _side_strength(rows, -1, n_videos, w)
        stats[author] = {
            "bull": bull,
            "bear": bear,
            "bull_videos": bull_videos,
            "bear_videos": bear_videos,
            "sent": sum(_sentiment(r) for r in rows) / len(rows),
            "ctx": sum(_context_score(r, w) for r in rows) / len(rows),
            "conv": _conviction(rows),
        }
    return stats


def _top_theses(mentions: list[dict], side: int, w: Weights) -> list[tuple]:
    """(channel, thesis) of one side's substantive theses, strongest (ctx x conviction) first."""
    rows = [r for r in mentions if _sentiment(r) == side and _substantive(r.get("thesis"))]
    rows.sort(key=lambda r: -_context_score(r, w) * float(r.get("conviction") or 1))
    return [(r.get("channel"), r.get("thesis")) for r in rows[:3]]


def _ticker_summary(ticker: str, mentions: list[dict], stats: dict, w: Weights) -> dict:
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
        "top_theses": {"bullish": _top_theses(mentions, 1, w), "bearish": _top_theses(mentions, -1, w)},
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
          weights: Weights = Weights()) -> dict:
    """Grade extracted ticker mentions into bullish/bearish leaderboards.

    Returns {"bullish": [...], "bearish": [...], "tickers": {ticker: summary},
    "disputed": {ticker: count}}. Rows the review dropped or left disputed add
    nothing to any ticker; disputed ones are only counted in `disputed`.
    `total_authors` is carried through for downstream breadth display.
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
        stats = _author_stats(mentions, videos_per_author, weights)
        if not stats:
            continue
        summaries[ticker] = _ticker_summary(ticker, mentions, stats, weights)

    all_summaries = list(summaries.values())
    return {
        "total_authors": total_authors,
        "bullish": _board(all_summaries, "bull_score"),
        "bearish": _board(all_summaries, "bear_score"),
        "tickers": summaries,
        "disputed": dict(disputed),
    }

