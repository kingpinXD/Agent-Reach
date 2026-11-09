# -*- coding: utf-8 -*-
"""The report: `tickers.json` and `report.md` from a graded result and the review files.

`report.md` keeps the leaderboard tables' column layout, so runs can be compared side by side,
and adds a "What the review changed" section. `review_changes` lists every finding that changed
the result (accepted) or stayed disputed, with the row's channel and the finding's quote.
"""

from __future__ import annotations

import glob
import os
import re

from agent_reach.stockyt import youtube
from agent_reach.stockyt.review import _outcome, chunk_files
from agent_reach.stockyt.run import atomic_write_json, atomic_write_text, read_json, read_jsonl

_APPLIED = {
    "wrong_ticker": "corrected",
    "wrong_sentiment": "corrected",
    "not_discussed": "dropped",
    "not_investable": "dropped",
    "missed_ticker": "added",
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


def console_lines(result: dict) -> list[str]:
    """Top 10 of each board for the terminal, with each side's breadth as on the report."""
    total = result["total_authors"]
    lines = []
    for side in ("bullish", "bearish"):
        short = side[:4]
        lines += ["", f"=== TOP 10 {side.upper()} ==="]
        for i, v in enumerate(result[side][:10], 1):
            flag = " CONTESTED" if v["contested"] else ""
            lines.append(f"{i:2}. {v['ticker']:6} {v['tier']} {v['score']:5} | "
                         f"{v[short + '_breadth']} {short} · {v['breadth']}/{total} auth{flag} | "
                         f"{str(v.get('company') or '')[:30]}")
    return lines


def _change(finding: dict, outcome: str, row: dict | None) -> dict:
    issue = finding["issue"]
    proposed = finding.get("proposed") or {}
    row = row or proposed
    ticker = finding.get("ticker")
    if issue == "wrong_ticker":
        original, new = ticker, proposed.get("ticker")
    elif issue == "wrong_sentiment":
        original, new = row.get("original_sentiment") or row.get("sentiment"), proposed.get("sentiment")
    elif issue == "missed_ticker":
        ticker = proposed.get("ticker")
        original, new = None, f"{ticker} {proposed.get('sentiment')}"
    else:
        original, new = ticker, "dropped"
    return {
        "channel": row.get("channel"),
        "video_id": finding["video_id"],
        "ticker": ticker,
        "issue": issue,
        "status": "disputed" if outcome == "disputed" else _APPLIED[issue],
        "original": original,
        "new": new,
        "quote": finding.get("quote"),
        "finding": finding["id"],
    }


def chunk_changes(r1: list[dict], resp1: list[dict], r2: dict, resp2: list[dict],
                  reviewed: list[dict]) -> list[dict]:
    """The chunk's accepted and disputed findings, each joined to its reviewed row via `review_note`."""
    verdicts = {v.get("id"): v.get("verdict") for v in r2.get("verdicts") or []}
    rounds = [(r1, resp1, verdicts), (r2.get("findings") or [], resp2, None)]
    changes = []
    for findings, responses, round_verdicts in rounds:
        by_id = {r.get("id"): r for r in responses}
        for finding in findings:
            outcome = _outcome(finding, by_id, round_verdicts, [])
            if outcome in (None, "withdrawn"):
                continue
            row = next((r for r in reviewed if r.get("video_id") == finding["video_id"]
                        and finding["id"] in r.get("review_note", [])), None)
            changes.append(_change(finding, outcome, row))
    return changes


def review_changes(chunk_dir: str) -> list[dict]:
    """`chunk_changes` for every reviewed chunk in `chunk_dir`, in chunk order."""
    changes = []
    for reviewed_path in sorted(glob.glob(os.path.join(chunk_dir, "reviewed_*.jsonl"))):
        nn = re.fullmatch(r"reviewed_(\d+)\.jsonl", os.path.basename(reviewed_path)).group(1)
        r1, resp1, r2, resp2 = (read_json(os.path.join(chunk_dir, name)) for name in chunk_files(nn)[1:])
        changes += chunk_changes(r1, resp1, r2, resp2, read_jsonl(reviewed_path))
    return changes


def _cell(value) -> str:
    return str(value if value is not None else "—").replace("|", "/")


def changes_markdown(changes: list[dict]) -> str:
    lines = ["## What the review changed", ""]
    if not changes:
        return "\n".join(lines + ["Nothing: every row was confirmed.", ""])
    lines += ["| Channel | Video | Ticker | Status | Change | Quote |", "|---|---|---|---|---|---|"]
    for c in changes:
        video = f"[{c['video_id']}]({youtube.watch_url(c['video_id'])})"
        lines.append(f"| {_cell(c['channel'])} | {video} | {_cell(c['ticker'])} | {c['status']} "
                     f"| {_cell(c['original'])} → {_cell(c['new'])} | {_cell(c['quote'])} |")
    return "\n".join(lines + [""])


def write(out_dir: str, result: dict, changes: list[dict]) -> tuple[str, str]:
    """Write tickers.json (the graded result plus `review_changes`) and report.md; return their paths."""
    json_path = os.path.join(out_dir, "tickers.json")
    md_path = os.path.join(out_dir, "report.md")
    atomic_write_json(json_path, {**result, "review_changes": changes})
    atomic_write_text(md_path, leaderboard_markdown(result) + "\n" + changes_markdown(changes))
    return json_path, md_path
