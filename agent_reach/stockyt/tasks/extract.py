# -*- coding: utf-8 -*-
"""Extraction task: one chunk of transcripts in, one row per (video, ticker) out."""

from __future__ import annotations

from agent_reach.stockyt.grading import _normalize_ticker
from agent_reach.stockyt.llm.base import LLMTask
from agent_reach.stockyt.llm.runner import Validator
from agent_reach.stockyt.llm.schema import schema_errors
from agent_reach.stockyt.tasks.quotes import quote_found

SENTIMENTS = ["bullish", "bearish", "neutral"]

ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "video_id": {"type": "string"},
        "channel": {"type": "string"},
        "ticker": {"type": ["string", "null"]},
        "company": {"type": "string"},
        "asset_type": {"type": "string", "enum": ["stock", "etf", "crypto", "index"]},
        "mentions": {"type": "integer"},
        "sentiment": {"type": "string", "enum": SENTIMENTS},
        "conviction": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "is_primary_topic": {"type": "boolean"},
        "recommendation": {
            "type": "string",
            "enum": ["strong_buy", "buy", "hold", "sell", "strong_sell", "watch", "none"],
        },
        "thesis": {"type": ["string", "null"]},
        "catalyst": {"type": ["string", "null"]},
        "time_horizon": {"type": "string", "enum": ["short", "medium", "long", "unspecified"]},
        "price_target": {"type": ["string", "null"]},
        "quote": {"type": "string"},
    },
    "required": [
        "video_id", "channel", "ticker", "company", "asset_type", "mentions", "sentiment",
        "conviction", "is_primary_topic", "recommendation", "thesis", "catalyst", "time_horizon",
        "price_target", "quote",
    ],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "videos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["done", "no_tickers"]},
                    "rows": {"type": "array", "items": ROW_SCHEMA},
                },
                "required": ["video_id", "status", "rows"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["videos"],
    "additionalProperties": False,
}

SYSTEM = (
    "You extract stock, ETF and crypto tickers from finance YouTube transcripts. "
    "You answer with one JSON value that matches the given schema, and nothing else."
)

QUOTE_RULE = (
    "`quote` must be words copied exactly from that video's transcript. To join separate "
    "passages, put \" ... \" between them."
)

GUEST_RULE = "Use the host's stance; a guest's view is neutral unless the host adopts it."

ROW_FIELDS = (
    '{"video_id":…,"channel":…,"ticker":"<OFFICIAL UPPERCASE SYMBOL>","company":"…",'
    '"asset_type":"stock|etf|crypto|index","mentions":<approx count in this video>,'
    '"sentiment":"bullish|bearish|neutral","conviction":<1-5>,"is_primary_topic":<bool>,'
    '"recommendation":"strong_buy|buy|hold|sell|strong_sell|watch|none",'
    '"thesis":"<≤240-char summary of WHY they hold this stance — the actual argument, not a '
    'description>","catalyst":"<specific driver/event they cite, or null>",'
    '"time_horizon":"short|medium|long|unspecified","price_target":<string or null>,'
    '"quote":"<≤200-char representative quote>"}'
)

EXTRACT_RULES = (
    "Rules: resolve the official ticker for well-known names (nvidia→NVDA, google→GOOGL, "
    "meta→META, amd→AMD, bitcoin→BTC); only use `ticker:null` if genuinely unresolvable. "
    "De-dupe aliases within a video to ONE row (sum the mentions). `is_primary_topic` = the video "
    "is substantially ABOUT this ticker, not a name-drop in a list. `sentiment` = the author's "
    "stance on the ticker in THIS video. "
    f"{GUEST_RULE} "
    "`conviction`: 5 = \"my #1 pick / loading up / table-pounding\", 1 = passing mention. "
    "**`thesis` is the most important field** — capture the author's actual reasoning; if they "
    "give NO reasoning (pure name-drop), set `thesis:null` and `conviction:1`. `catalyst` = the "
    "concrete reason it moves (earnings, product, Fed, regulation…), null if none stated. Skip "
    "generic market talk with no specific ticker. Do NOT summarize transcripts. "
    f"{QUOTE_RULE}"
)

INDIA_RULE = (
    "Indian market: give an Indian company its NSE symbol (Reliance→RELIANCE, HDFC Bank→HDFCBANK, "
    "Infosys→INFY, Tata Motors→TATAMOTORS), also when its name is written in Devanagari; the "
    "indices are NIFTY, BANKNIFTY and SENSEX. Transcripts may be in Hindi, or English written in "
    "Devanagari: write `company`, `thesis` and `catalyst` in English, and copy `quote` verbatim in "
    "the transcript's own script."
)

MARKET_RULES: dict[str, str | None] = {"us": None, "india": INDIA_RULE}


def market_rules(market: str) -> list[str]:
    """The market's extra prompt section, if it has one."""
    rule = MARKET_RULES[market]
    return [rule] if rule else []


def render_videos(records: list[dict]) -> str:
    """The chunk's transcripts, one block per video."""
    return "\n\n".join(
        f"=== VIDEO video_id={r['video_id']} channel={r['channel']} ===\n{r['transcript']}"
        for r in records
    )


def render_prompt(records: list[dict], market: str = "us") -> str:
    return "\n\n".join([
        "Read every video below. For each video's transcript, identify every "
        "**publicly-investable** ticker discussed (stocks, ETFs, major crypto). For each "
        "(video, ticker) emit ONE row:",
        ROW_FIELDS,
        EXTRACT_RULES,
        *market_rules(market),
        'Answer {"videos":[{"video_id","status","rows"}]}: exactly one entry per video below. '
        '`status` is "done" when the video has rows, or "no_tickers" with `rows: []` when it '
        "discusses no specific ticker. Each row's `video_id` and `channel` are its video's.",
        render_videos(records),
    ])


def task(task_id: str, records: list[dict], market: str = "us") -> LLMTask:
    return LLMTask(id=task_id, prompt=render_prompt(records, market), schema=SCHEMA, system=SYSTEM)


def row_errors(row: dict, video: dict, where: str) -> list[str]:
    """One extract row checked against its video record: schema, ids, channel, the quote."""
    errors = schema_errors(row, ROW_SCHEMA, where)
    if errors:
        return errors
    if row["video_id"] != video["video_id"]:
        errors.append(f"{where}: video_id {row['video_id']!r} is not {video['video_id']!r}")
    if row["channel"] != video["channel"]:
        errors.append(f"{where}: channel {row['channel']!r} is not {video['channel']!r}")
    if not quote_found(row["quote"], video["transcript"]):
        errors.append(f"{where}: quote is not in the transcript: {row['quote']!r}")
    return errors


def validate(answer: dict, records: list[dict]) -> list[str]:
    """Every chunk video exactly once, rows consistent with their video, quotes verbatim.

    Runs after the runner's schema check, so the answer already has the schema's shape.
    """
    by_id = {r["video_id"]: r for r in records}
    errors: list[str] = []
    seen: list[str] = []
    for i, video in enumerate(answer["videos"]):
        vid = video["video_id"]
        where = f"videos[{i}] ({vid})"
        if vid not in by_id:
            errors.append(f"{where}: not a video of this chunk")
            continue
        seen.append(vid)
        rows = video["rows"]
        if (video["status"] == "no_tickers") != (not rows):
            errors.append(f"{where}: status {video['status']!r} does not match {len(rows)} rows")
        tickers = [t for t in (_normalize_ticker(row) for row in rows if row.get("ticker")) if t]
        errors += [f"{where}: ticker {t} has more than one row" for t in sorted(set(tickers))
                   if tickers.count(t) > 1]
        for j, row in enumerate(rows):
            errors += row_errors(row, by_id[vid], f"{where}.rows[{j}]")
    errors += [f"video {vid}: answered more than once" for vid in sorted(set(seen))
               if seen.count(vid) > 1]
    errors += [f"video {vid}: missing from the answer" for vid in by_id if vid not in seen]
    return errors


def to_rows(answer: dict) -> list[dict]:
    """The answer as extract_NN.jsonl rows."""
    return [row for video in answer["videos"] for row in video["rows"]]


def job(task_id: str, records: list[dict], market: str = "us") -> tuple[LLMTask, Validator]:
    """The task and its validator, ready for `runner.ask` / `ask_many`."""
    return task(task_id, records, market), lambda answer: validate(answer, records)
