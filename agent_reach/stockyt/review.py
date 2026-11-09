# -*- coding: utf-8 -*-
"""Apply the two-round ticker review to the extracted rows. Agents judge; this code decides.

Per chunk NN the run dir holds:
  extract_NN.jsonl        the extractor's rows
  review_NN_r1.json       [{id, video_id, ticker, issue, proposed, quote}]
  response_NN_r1.json     [{id, decision: accept|reject, quote?, reason}]
  review_NN_r2.json       {"verdicts": [{id, verdict: hold|withdraw}], "findings": [<"r2-" findings>]}
  response_NN_r2.json     [{id, decision, quote?, reason}] for the r2- findings
and this module writes reviewed_NN.jsonl: every extract row (plus added rows) with
`review_status` and `review_note` (the ids of the findings that touched the row).

A finding is accepted (last response accepts), withdrawn (rejected, then withdrawn in R2)
or disputed (rejected and held in R2, or an r2- finding rejected). R1 applies to the
extract, then R2 to the result. Status precedence on one row:
dropped > disputed > added > corrected > confirmed.
"""

from __future__ import annotations

import glob
import json
import os
import re

from agent_reach.stockyt.grading import _normalize_ticker

ISSUES = ("wrong_ticker", "wrong_sentiment", "not_discussed", "not_investable", "missed_ticker")
_RANK = {"confirmed": 0, "corrected": 1, "added": 2, "disputed": 3, "dropped": 4}
STATUSES = tuple(_RANK)


def _key(video_id, ticker) -> tuple:
    return video_id, _normalize_ticker({"ticker": ticker})


def _outcome(finding: dict, responses: dict, verdicts: dict | None, problems: list) -> str | None:
    """accepted | withdrawn | disputed, or None (and a problem) when the exchange is incomplete."""
    fid = finding.get("id")
    decision = (responses.get(fid) or {}).get("decision")
    if decision == "accept":
        return "accepted"
    if decision != "reject":
        problems.append(f"finding {fid}: no accept/reject response")
        return None
    if verdicts is None:  # an r2- finding has no third round: a rejection stands as a dispute
        return "disputed"
    verdict = verdicts.get(fid)
    if verdict == "withdraw":
        return "withdrawn"
    if verdict == "hold":
        return "disputed"
    problems.append(f"finding {fid}: rejected in R1 but has no hold/withdraw verdict in R2")
    return None


def _mark(row: dict, status: str, fid: str) -> None:
    if _RANK[status] > _RANK[row["review_status"]]:
        row["review_status"] = status
    if fid not in row["review_note"]:
        row["review_note"].append(fid)


def _correct(row: dict, finding: dict, problems: list) -> None:
    proposed = finding.get("proposed") or {}
    fid = finding["id"]
    if finding["issue"] == "wrong_ticker":
        if not proposed.get("ticker"):
            problems.append(f"finding {fid}: wrong_ticker without a proposed ticker")
            return
        row.setdefault("original_ticker", row.get("ticker"))
        for field in ("ticker", "company", "asset_type"):
            if field in proposed:
                row[field] = proposed[field]
    else:
        if not proposed.get("sentiment"):
            problems.append(f"finding {fid}: wrong_sentiment without a proposed sentiment")
            return
        row.setdefault("original_sentiment", row.get("sentiment"))
        row["sentiment"] = proposed["sentiment"]
    _mark(row, "corrected", fid)


def _apply_round(rows: list[dict], findings: list[dict], responses: dict, verdicts: dict | None,
                 problems: list) -> int:
    """Apply one round's findings to `rows` in place; return how many missed tickers stayed disputed."""
    index: dict[tuple, list[dict]] = {}
    for row in rows:
        index.setdefault(_key(row.get("video_id"), row.get("ticker")), []).append(row)

    disputed_missed = 0
    for finding in findings:
        fid, issue = finding.get("id"), finding.get("issue")
        if issue not in ISSUES:
            problems.append(f"finding {fid}: unknown issue {issue!r}")
            continue
        outcome = _outcome(finding, responses, verdicts, problems)
        if outcome is None:
            continue

        if issue == "missed_ticker":
            if outcome == "disputed":
                disputed_missed += 1
            if outcome != "accepted":
                continue
            added = {**(finding.get("proposed") or {})}
            added["video_id"] = finding.get("video_id") or added.get("video_id")
            if not (added.get("video_id") and added.get("ticker") and added.get("channel")):
                problems.append(f"finding {fid}: missed_ticker proposal needs video_id, ticker and channel")
                continue
            key = _key(added["video_id"], added["ticker"])
            if key in index:
                problems.append(f"finding {fid}: missed_ticker {key[1]} already exists for video {key[0]}")
                continue
            added.update({"review_status": "added", "review_note": [fid]})
            rows.append(added)
            index[key] = [added]
            continue

        targets = index.get(_key(finding.get("video_id"), finding.get("ticker")))
        if not targets:
            problems.append(f"finding {fid}: no row for video {finding.get('video_id')} / {finding.get('ticker')}")
            continue
        for row in targets:
            if outcome == "withdrawn":
                _mark(row, "confirmed", fid)
            elif outcome == "disputed":
                _mark(row, "disputed", fid)
            elif issue in ("not_discussed", "not_investable"):
                _mark(row, "dropped", fid)
            else:
                _correct(row, finding, problems)
    return disputed_missed


def apply_review(extract_rows: list[dict], r1: list[dict], resp1: list[dict], r2: dict,
                 resp2: list[dict]) -> tuple[list[dict], list[str], int]:
    """Reviewed rows, problems, and the number of disputed missed tickers (which add no row).

    The rows are only meaningful when `problems` is empty.
    """
    problems: list[str] = []
    rows = [{**row, "review_status": "confirmed", "review_note": []} for row in extract_rows]
    verdicts = {v.get("id"): v.get("verdict") for v in r2.get("verdicts") or []}
    r2_findings = r2.get("findings") or []
    for finding in r2_findings:
        if not str(finding.get("id", "")).startswith("r2-"):
            problems.append(f"R2 finding {finding.get('id')}: id must start with 'r2-'")

    disputed_missed = _apply_round(rows, r1, {r.get("id"): r for r in resp1}, verdicts, problems)
    disputed_missed += _apply_round(rows, r2_findings, {r.get("id"): r for r in resp2}, None, problems)
    return rows, problems, disputed_missed


def _load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def review_run(run_dir: str) -> dict:
    """Write reviewed_NN.jsonl for every chunk in `run_dir` whose review is complete; return counts.

    A chunk with a missing or unreadable file, or any problem, gets no reviewed file.
    The counts are also written to `review_summary.json` so `grade --reviewed` can embed them.
    """
    counts: dict = {"chunks": 0, "reviewed_chunks": 0, **{s: 0 for s in STATUSES},
                    "disputed_missed": 0, "problems": []}
    chunk_paths = sorted(glob.glob(os.path.join(run_dir, "chunk_*.jsonl")))
    counts["chunks"] = len(chunk_paths)
    if not chunk_paths:
        counts["problems"].append(f"no chunk_*.jsonl in {run_dir}")

    for chunk_path in chunk_paths:
        nn = re.fullmatch(r"chunk_(\d+)\.jsonl", os.path.basename(chunk_path)).group(1)
        names = [f"extract_{nn}.jsonl", f"review_{nn}_r1.json", f"response_{nn}_r1.json",
                 f"review_{nn}_r2.json", f"response_{nn}_r2.json"]
        paths = [os.path.join(run_dir, name) for name in names]
        missing = [name for name, path in zip(names, paths) if not os.path.exists(path)]
        if missing:
            counts["problems"].append(f"chunk {nn}: missing {', '.join(missing)}")
            continue
        try:
            extract = _load_jsonl(paths[0])
            r1, resp1, r2, resp2 = (_load_json(p) for p in paths[1:])
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            counts["problems"].append(f"chunk {nn}: unreadable file — {e}")
            continue
        if not (isinstance(r1, list) and isinstance(resp1, list) and isinstance(r2, dict)
                and isinstance(resp2, list)):
            counts["problems"].append(f"chunk {nn}: review files must be lists, review_{nn}_r2.json an object")
            continue

        rows, problems, disputed_missed = apply_review(extract, r1, resp1, r2, resp2)
        if problems:
            counts["problems"].extend(f"chunk {nn}: {p}" for p in problems)
            continue
        with open(os.path.join(run_dir, f"reviewed_{nn}.jsonl"), "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        counts["reviewed_chunks"] += 1
        counts["disputed_missed"] += disputed_missed
        for row in rows:
            counts[row["review_status"]] += 1

    counts["ok"] = not counts["problems"] and counts["reviewed_chunks"] == counts["chunks"]
    with open(os.path.join(run_dir, "review_summary.json"), "w", encoding="utf-8") as f:
        json.dump(counts, f, ensure_ascii=False, indent=2)
    return counts
