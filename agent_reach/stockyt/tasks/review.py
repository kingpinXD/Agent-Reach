# -*- coding: utf-8 -*-
"""The four review calls per chunk: R1 review, R1 response, R2 review, R2 response.

Each call is stateless: its prompt carries the transcripts and every earlier file it needs.
`to_file` turns an answer into today's file shape (`review_NN_r1.json` list,
`response_NN_rN.json` list, `review_NN_r2.json` {verdicts, findings}), so
`agent_reach.stockyt.review.apply_review` consumes them unchanged.
"""

from __future__ import annotations

import json
import re

from agent_reach.stockyt.llm.base import LLMTask
from agent_reach.stockyt.llm.runner import Validator
from agent_reach.stockyt.review import ISSUES, _key, apply_review
from agent_reach.stockyt.tasks.extract import (
    GUEST_RULE,
    QUOTE_RULE,
    ROW_FIELDS,
    SENTIMENTS,
    render_videos,
    row_errors,
)
from agent_reach.stockyt.tasks.quotes import quote_found

FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "video_id": {"type": "string"},
        "ticker": {"type": ["string", "null"]},
        "issue": {"type": "string", "enum": list(ISSUES)},
        "proposed": {"type": "object"},
        "quote": {"type": "string"},
    },
    "required": ["id", "video_id", "ticker", "issue", "proposed", "quote"],
    "additionalProperties": False,
}

R1_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": FINDING_SCHEMA}},
    "required": ["findings"],
    "additionalProperties": False,
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "responses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "decision": {"type": "string", "enum": ["accept", "reject"]},
                    "quote": {"type": ["string", "null"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "decision", "quote", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["responses"],
    "additionalProperties": False,
}

R2_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["hold", "withdraw"]},
                },
                "required": ["id", "verdict"],
                "additionalProperties": False,
            },
        },
        "findings": {"type": "array", "items": FINDING_SCHEMA},
    },
    "required": ["verdicts", "findings"],
    "additionalProperties": False,
}

REVIEWER_SYSTEM = (
    "You review another agent's ticker extraction from finance YouTube transcripts. "
    "You answer with one JSON value that matches the given schema, and nothing else."
)

EXTRACTOR_SYSTEM = (
    "You extracted stock, ETF and crypto tickers from finance YouTube transcripts, and now "
    "answer a reviewer's findings. You answer with one JSON value that matches the given "
    "schema, and nothing else."
)

FINDING_FIELDS = "\n".join([
    "Finding fields:",
    "- `issue` is one of `wrong_ticker` · `wrong_sentiment` · `not_discussed` (the ticker is "
    "never said in the video; a passing name-drop is said, and the extraction rules keep it as a "
    "row) · `not_investable` (not a publicly-investable stock/ETF/crypto/index) · "
    "`missed_ticker` (a discussed ticker with no row).",
    "- `video_id` + `ticker` name the extract row the finding is about (`ticker` is `null` for "
    "`missed_ticker`).",
    '- `proposed` is `{"ticker","company","asset_type"}` for `wrong_ticker`, `{"sentiment"}` for '
    "`wrong_sentiment`, a **full extract row** (same schema as the extraction, including "
    "`video_id` and `channel`) for `missed_ticker`, and `{}` otherwise. An extract row is: "
    f"{ROW_FIELDS}",
    "- `quote` is **required**: verbatim words from the transcript that support the finding.",
])

EMPTY_R2: dict[str, list] = {"verdicts": [], "findings": []}

_F_ID = re.compile(r"f\d+")
_R2_ID = re.compile(r"r2-\d+")


def _jsonl(items: list[dict]) -> str:
    return "\n".join(json.dumps(item, ensure_ascii=False) for item in items) or "(none)"


def _sections(*parts: str) -> str:
    return "\n\n".join(parts)


def r1_review_prompt(records: list[dict], rows: list[dict]) -> str:
    return _sections(
        "You are reviewing another agent's ticker extraction. Read every video's transcript and "
        "every row of the extraction below. Check two things for each video: (a) the tickers — is "
        "each row's ticker the right official symbol for what was discussed, was it really "
        "discussed, is it publicly investable, and is any discussed ticker missing; (b) the "
        "sentiment — is each row's `sentiment` the author's actual stance in THIS video. "
        f"{GUEST_RULE} Report only real errors you can prove with a verbatim quote from the "
        "transcript; do not report style, thesis wording, conviction or mention counts. Answer "
        '{"findings":[…]} using ids `f1`, `f2`, … and exactly these fields: `id, video_id, '
        'ticker, issue, proposed, quote`. Answer {"findings":[]} if everything holds.',
        FINDING_FIELDS,
        QUOTE_RULE,
        "=== EXTRACTION ===\n" + _jsonl(rows),
        render_videos(records),
    )


def response_prompt(records: list[dict], rows: list[dict], findings: list[dict],
                    last_round: bool) -> str:
    rounds = ("This is the last round: there is no round 3." if last_round else
              "The reviewer answers your rejections in a second round.")
    return _sections(
        "You extracted the tickers below, and a reviewer checked your extraction. Read the "
        "reviewer's findings and the transcripts again. For every finding decide `accept` (the "
        "reviewer is right) or `reject` (you are right; give a verbatim transcript `quote` that "
        'shows it). Answer {"responses":[…]} with exactly one {"id","decision","quote","reason"} '
        "per finding. `quote` may be null only when you accept. You do not edit the extraction: "
        f"the program applies accepted findings. {rounds}",
        QUOTE_RULE,
        "=== EXTRACTION ===\n" + _jsonl(rows),
        "=== FINDINGS ===\n" + _jsonl(findings),
        render_videos(records),
    )


def r2_review_prompt(records: list[dict], rows_after_r1: list[dict], r1: list[dict],
                     resp1: list[dict]) -> str:
    return _sections(
        "You reviewed another agent's ticker extraction and wrote the round-1 findings below. The "
        "extractor answered them in the round-1 responses below. The extraction below already "
        "has your accepted findings applied (`review_status`, `original_ticker` and "
        '`original_sentiment` show what changed). Answer {"verdicts":[…],"findings":[…]}. '
        '`verdicts`: exactly one {"id","verdict"} for every finding the extractor rejected — '
        "`withdraw` if their quote convinces you, else `hold`. `findings`: new findings only "
        "about rows your accepted findings changed (the corrected ticker or sentiment, or a row "
        "you proposed as `missed_ticker`), same fields as before, ids `r2-1`, `r2-2`, …; "
        f"usually []. {GUEST_RULE}",
        FINDING_FIELDS,
        QUOTE_RULE,
        "=== EXTRACTION ===\n" + _jsonl(rows_after_r1),
        "=== ROUND-1 FINDINGS ===\n" + _jsonl(r1),
        "=== ROUND-1 RESPONSES ===\n" + _jsonl(resp1),
        render_videos(records),
    )


def rows_after_r1(extract_rows: list[dict], r1: list[dict], resp1: list[dict]) -> list[dict]:
    """The extract rows with the accepted R1 findings applied, as R2 sees them."""
    rows, _, _ = apply_review(extract_rows, r1, resp1, EMPTY_R2, [])
    return rows


def _id_errors(items: list[dict], pattern: re.Pattern, label: str) -> list[str]:
    ids = [item["id"] for item in items]
    errors = [f"{label} id {i!r} is not like {pattern.pattern}" for i in ids
              if not pattern.fullmatch(i)]
    errors += [f"{label} id {i!r} is used more than once" for i in sorted(set(ids))
               if ids.count(i) > 1]
    return errors


def _finding_errors(finding: dict, videos: dict[str, dict], rows: list[dict]) -> list[str]:
    fid, issue, vid = finding["id"], finding["issue"], finding["video_id"]
    video = videos.get(vid)
    if video is None:
        return [f"finding {fid}: video {vid!r} is not in this chunk"]
    errors = []
    if not quote_found(finding["quote"], video["transcript"]):
        errors.append(f"finding {fid}: quote is not in the transcript: {finding['quote']!r}")

    keys = {_key(row.get("video_id"), row.get("ticker")) for row in rows}
    proposed = finding["proposed"]
    if issue == "missed_ticker":
        errors += row_errors(proposed, video, f"finding {fid}: proposed")
        if _key(vid, proposed.get("ticker")) in keys:
            errors.append(f"finding {fid}: {proposed.get('ticker')} already has a row in {vid}")
        return errors

    if _key(vid, finding["ticker"]) not in keys:
        errors.append(f"finding {fid}: no extract row for {vid} / {finding['ticker']}")
    if issue == "wrong_ticker" and not (isinstance(proposed.get("ticker"), str) and proposed["ticker"]):
        errors.append(f"finding {fid}: wrong_ticker needs a proposed ticker")
    if issue == "wrong_sentiment" and proposed.get("sentiment") not in SENTIMENTS:
        errors.append(f"finding {fid}: wrong_sentiment needs a proposed sentiment in {SENTIMENTS}")
    return errors


def _findings_errors(findings: list[dict], pattern: re.Pattern, records: list[dict],
                     rows: list[dict]) -> list[str]:
    videos = {r["video_id"]: r for r in records}
    errors = _id_errors(findings, pattern, "finding")
    for finding in findings:
        errors += _finding_errors(finding, videos, rows)
    return errors


def validate_r1_review(answer: dict, records: list[dict], rows: list[dict]) -> list[str]:
    return _findings_errors(answer["findings"], _F_ID, records, rows)


def validate_response(answer: dict, records: list[dict], findings: list[dict]) -> list[str]:
    """One response per finding; a rejection carries a quote; every quote is in its finding's video."""
    responses = answer["responses"]
    by_finding = {f["id"]: f for f in findings}
    ids = [r["id"] for r in responses]
    errors = [f"response to unknown finding {i!r}" for i in ids if i not in by_finding]
    errors += [f"finding {i!r} answered more than once" for i in sorted(set(ids)) if ids.count(i) > 1]
    errors += [f"finding {i!r} has no response" for i in by_finding if i not in ids]

    transcripts = {r["video_id"]: r["transcript"] for r in records}
    for response in responses:
        finding = by_finding.get(response["id"])
        if finding is None:
            continue
        quote = response["quote"]
        if response["decision"] == "reject" and not quote:
            errors.append(f"response {response['id']}: a rejection needs a quote")
        if quote and not quote_found(quote, transcripts.get(finding["video_id"], "")):
            errors.append(f"response {response['id']}: quote is not in the transcript: {quote!r}")
    return errors


def validate_r2_review(answer: dict, records: list[dict], rows_after: list[dict], r1: list[dict],
                       resp1: list[dict]) -> list[str]:
    """One verdict per rejected R1 finding and no other; R2 findings checked like R1 findings."""
    rejected = {r["id"] for r in resp1 if r.get("decision") == "reject"}
    ids = [v["id"] for v in answer["verdicts"]]
    errors = [f"verdict for {i!r}, which was not rejected" for i in ids if i not in rejected]
    errors += [f"finding {i!r} has more than one verdict" for i in sorted(set(ids)) if ids.count(i) > 1]
    errors += [f"rejected finding {i!r} has no verdict" for i in sorted(rejected) if i not in ids]
    return errors + _findings_errors(answer["findings"], _R2_ID, records, rows_after)


def r1_review_job(task_id: str, records: list[dict], rows: list[dict]) -> tuple[LLMTask, Validator]:
    task = LLMTask(task_id, r1_review_prompt(records, rows), R1_REVIEW_SCHEMA, REVIEWER_SYSTEM)
    return task, lambda answer: validate_r1_review(answer, records, rows)


def response_job(task_id: str, records: list[dict], rows: list[dict], findings: list[dict],
                 last_round: bool) -> tuple[LLMTask, Validator]:
    prompt = response_prompt(records, rows, findings, last_round)
    task = LLMTask(task_id, prompt, RESPONSE_SCHEMA, EXTRACTOR_SYSTEM)
    return task, lambda answer: validate_response(answer, records, findings)


def r2_review_job(task_id: str, records: list[dict], extract_rows: list[dict], r1: list[dict],
                  resp1: list[dict]) -> tuple[LLMTask, Validator]:
    rows_after = rows_after_r1(extract_rows, r1, resp1)
    task = LLMTask(task_id, r2_review_prompt(records, rows_after, r1, resp1), R2_REVIEW_SCHEMA,
                   REVIEWER_SYSTEM)
    return task, lambda answer: validate_r2_review(answer, records, rows_after, r1, resp1)


def to_file(answer: dict):
    """The answer in today's file shape: R1 review → findings list, response → list, R2 → as is."""
    if "verdicts" in answer:
        return answer
    if "responses" in answer:
        return answer["responses"]
    return answer["findings"]
