# -*- coding: utf-8 -*-
"""`agent-reach stockyt <command>` — the finance-YouTube pipeline commands.

channels → transcripts → split-corpus → (LLM extraction) → grade. Prose output for
humans and skills, not the JSON envelope the rest of agent-reach prints.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta

_EXIT_CODES = {"done": 0, "failed": 1, "blocked": 2}


def _since_value(value: str) -> str:
    """argparse type for --since: YYYYMMDD or YYYYMMDDTHHMM, not in the future. The time of day is
    read in the configured timezone later, by channels.parse_since."""
    from agent_reach.stockyt.channels import parse_since

    try:
        day = parse_since(value, "UTC").day
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    if day > date.today():
        raise argparse.ArgumentTypeError(f"{value} is in the future")
    return value


def _day_value(value: str) -> str:
    """argparse type for --until: a real YYYYMMDD day."""
    try:
        if len(value) != 8 or not value.isdigit():
            raise ValueError
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYYMMDD date: {value!r}") from None
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-reach stockyt",
                                     description="Finance-YouTube pipeline (deterministic steps)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ch = sub.add_parser("channels", help="List finance-channel uploads by recency into a links file")
    window = p_ch.add_mutually_exclusive_group(required=True)
    window.add_argument("--weeks", type=int, help="Keep videos uploaded within the last N weeks")
    window.add_argument("--since", type=_since_value,
                        help="Keep videos uploaded from this day (YYYYMMDD) or moment (YYYYMMDDTHHMM, "
                             "in the configured timezone)")
    p_ch.add_argument("--until", type=_day_value, default=None,
                      help="Keep videos uploaded on or before this day (YYYYMMDD)")
    p_ch.add_argument("-o", "--output", default=None, help="Write watch URLs here, one per line")
    p_ch.add_argument("--cache", default=None,
                      help="Cache directory (default: <output_dir>/.cache/channels); read only when a listing fails")
    p_ch.add_argument("--channels-json", default=None, help="Also write a structured per-channel JSON here")
    p_ch.add_argument("--no-cache", action="store_true", help="Neither read nor update the cache")

    p_tr = sub.add_parser("transcripts", help="Fetch YouTube transcripts to JSONL, one video at a time")
    p_tr.add_argument("url", nargs="?", default=None, help="A single YouTube URL")
    p_tr.add_argument("--file", default=None,
                      help="File of YouTube URLs, one per line (# comments and blank lines skipped)")
    p_tr.add_argument("-o", "--output", default=None,
                      help="Write JSONL here (re-running skips videos already ok); default: stdout")
    p_tr.add_argument("--lang", default=None, help="Caption language; <lang>-orig is preferred (default: en)")
    whisper = p_tr.add_mutually_exclusive_group()
    whisper.add_argument("--whisper", dest="whisper", action="store_true", default=None,
                         help="Fall back to Whisper when a video has no captions")
    whisper.add_argument("--no-whisper", dest="whisper", action="store_false", help="Captions only (default)")
    p_tr.add_argument("--cookies-from-browser", default=None,
                      help="Send this browser's YouTube cookies (only with the user's OK)")

    p_sc = sub.add_parser("split-corpus", help="Split a transcripts JSONL into authors.json + chunk_*.jsonl")
    p_sc.add_argument("--input", required=True, help="transcripts.jsonl from the transcripts command")
    p_sc.add_argument("--out-dir", required=True, help="Directory for authors.json + chunk_*.jsonl")
    p_sc.add_argument("--chunk-chars", type=int, default=None,
                      help="Transcript characters per chunk (default: the configured chunk_chars)")

    p_ra = sub.add_parser("review-apply",
                          help="Apply the two-round ticker review: write reviewed_*.jsonl from the review files")
    p_ra.add_argument("--run-dir", required=True, help="Run directory with chunk/extract/review/response files")

    p_gr = sub.add_parser("grade", help="Grade extracted ticker mentions into ranked leaderboards")
    p_gr.add_argument("--extract-dir", required=True, help="Directory of extract_*.jsonl ticker rows")
    p_gr.add_argument("--authors", required=True, help="authors.json with videos_per_author + total_authors")
    p_gr.add_argument("-o", "--output", required=True, help="Write the graded leaderboard JSON here")
    p_gr.add_argument("--reviewed", action="store_true",
                      help="Grade reviewed_*.jsonl (from review-apply) instead of extract_*.jsonl")
    return parser


def main(argv: Sequence[str]) -> int:
    args = _parser().parse_args(list(argv))
    handlers = {
        "channels": _cmd_channels,
        "transcripts": _cmd_transcripts,
        "split-corpus": _cmd_split_corpus,
        "review-apply": _cmd_review_apply,
        "grade": _cmd_grade,
    }
    return handlers[args.command](args)


def _read_jsonl(path: str) -> list[dict]:
    """Rows of a JSONL file; a malformed line is skipped, never fatal."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _cmd_channels(args) -> int:
    """List each tracked finance channel's uploads in the window into a links file."""
    from agent_reach.stockyt import channels as channels_mod
    from agent_reach.stockyt.config import load_settings
    from agent_reach.stockyt.run import atomic_write_json, atomic_write_text

    settings = load_settings()
    if args.since:
        cutoff = channels_mod.parse_since(args.since, settings.timezone)
    else:
        cutoff = channels_mod.Cutoff(date.today() - timedelta(weeks=args.weeks))
    cache_dir = None if args.no_cache else (args.cache or settings.cache_dir)

    results = channels_mod.sweep(settings.channels, cutoff, until=args.until, cache_dir=cache_dir,
                                 pace_seconds=settings.pace_seconds)
    for r in results:
        if not r.error:
            print(f"  {r.name}: {len(r.rows)} videos, oldest {r.rows[-1]['upload_date'] if r.rows else '—'}")
    status, lines = channels_mod.check(results)
    for line in lines:
        print(line)

    urls = channels_mod.window_urls(results)
    if args.output:
        atomic_write_text(args.output, "".join(url + "\n" for url in urls))
        print(f"✅ {len(urls)} URLs → {args.output}")
    if args.channels_json:
        atomic_write_json(args.channels_json, [
            {"channel": r.name, "channel_id": r.channel_id, "count": len(r.rows),
             "oldest_date": r.rows[-1]["upload_date"] if r.rows else None, "reached_cutoff": r.reached_cutoff,
             "error": r.error, "videos": r.rows}
            for r in results
        ])
        print(f"✅ Structured channels → {args.channels_json}")
    return _EXIT_CODES[status]


def _cmd_transcripts(args) -> int:
    """Fetch transcripts one video at a time into JSONL; exit 1 on missing rows, 2 on a rate limit."""
    import dataclasses
    import tempfile

    from agent_reach.stockyt import transcripts
    from agent_reach.stockyt.config import load_settings

    if bool(args.url) == bool(args.file):
        print("❌ Provide exactly one of a URL argument or --file", file=sys.stderr)
        return 1

    settings = load_settings()
    overrides = {"lang": args.lang, "whisper": args.whisper, "cookies_from_browser": args.cookies_from_browser}
    settings = dataclasses.replace(settings, **{k: v for k, v in overrides.items() if v is not None})
    links = transcripts.read_link_file(args.file) if args.file else [args.url]

    if args.output:
        rows = transcripts.fetch_all(links, args.output, settings)
    else:
        with tempfile.TemporaryDirectory(prefix="stockyt-transcripts-") as tmp:
            rows = transcripts.fetch_all(links, os.path.join(tmp, "transcripts.jsonl"), settings)
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))

    status, lines = transcripts.check(links, rows)
    for line in lines:
        print(line, file=sys.stderr)
    print(f"→ {args.output or 'stdout'}", file=sys.stderr)
    return _EXIT_CODES[status]


def _cmd_split_corpus(args) -> int:
    """Split a transcripts JSONL into authors.json + contiguous chunk files."""
    from agent_reach.stockyt.config import load_settings
    from agent_reach.stockyt.corpus import split_corpus

    chunk_chars = args.chunk_chars or load_settings().chunk_chars
    authors, num_chunks = split_corpus(_read_jsonl(args.input), args.out_dir, chunk_chars)

    print(f"Kept {authors['N']} ok records (one per video) → {args.out_dir}")
    print(f"Authors: {authors['total_authors']}")
    for channel, count in sorted(authors["videos_per_author"].items(), key=lambda kv: -kv[1]):
        print(f"  {channel}: {count}")
    print(f"Wrote {num_chunks} chunks (~{chunk_chars} transcript characters each at most, "
          "or one longer video) + authors.json")
    return 0


def _cmd_review_apply(args) -> int:
    """Turn each chunk's extract + review files into reviewed_NN.jsonl."""
    from agent_reach.stockyt.review import review_run

    counts = review_run(args.run_dir)
    print(f"Reviewed {counts['reviewed_chunks']}/{counts['chunks']} chunks: "
          f"{counts['confirmed']} confirmed · {counts['corrected']} corrected · {counts['added']} added · "
          f"{counts['dropped']} dropped · {counts['disputed']} disputed "
          f"(+{counts['disputed_missed']} disputed missed tickers)")
    for problem in counts["problems"]:
        print(f"  ❌ {problem}")
    print("Review: OK" if counts["ok"] else "Review: FAIL — fix the chunks above and re-run review-apply")
    return 0 if counts["ok"] else 1


def _reviewed_rows(run_dir: str) -> tuple[list[dict], dict]:
    """Rows of every reviewed_NN.jsonl plus the review counts; raises ValueError if the review is incomplete."""
    summary_path = os.path.join(run_dir, "review_summary.json")
    if not os.path.exists(summary_path):
        raise ValueError("no review_summary.json — run `agent-reach stockyt review-apply` first")
    with open(summary_path, encoding="utf-8") as f:
        summary = json.load(f)
    if not summary.get("ok"):
        raise ValueError("review_summary.json says the review is not ok — fix it and re-run review-apply")
    rows = []
    missing = []
    for extract_path in sorted(glob.glob(os.path.join(run_dir, "extract_*.jsonl"))):
        reviewed_path = os.path.join(run_dir, os.path.basename(extract_path).replace("extract_", "reviewed_", 1))
        if not os.path.exists(reviewed_path):
            missing.append(os.path.basename(reviewed_path))
            continue
        rows.extend(_read_jsonl(reviewed_path))
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    return rows, summary


def _cmd_grade(args) -> int:
    """Grade extracted (or reviewed) ticker rows into bullish/bearish leaderboards."""
    from agent_reach.stockyt.grading import grade, integrity, leaderboard_markdown, review_line

    review = None
    if args.reviewed:
        try:
            extract_rows, review = _reviewed_rows(args.extract_dir)
        except ValueError as e:
            print(f"❌ Review: FAIL — {e}", file=sys.stderr)
            return 1
    else:
        extract_rows = []
        for path in sorted(glob.glob(os.path.join(args.extract_dir, "extract_*.jsonl"))):
            extract_rows.extend(_read_jsonl(path))

    with open(args.authors, encoding="utf-8") as f:
        authors = json.load(f)

    result = grade(extract_rows, authors["videos_per_author"], authors["total_authors"])
    if review is not None:
        result["review"] = review

    integ = integrity(args.extract_dir, authors.get("N", 0))
    if integ is not None:
        result["integrity"] = integ

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    md_path = os.path.splitext(args.output)[0] + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(leaderboard_markdown(result))

    print(f"Graded {len(extract_rows)} rows → {len(result['tickers'])} tickers")
    print("\n=== TOP 10 BULLISH ===")
    for i, v in enumerate(result["bullish"][:10], 1):
        flag = " CONTESTED" if v["contested"] else ""
        print(f"{i:2}. {v['ticker']:6} {v['tier']} {v['score']:5} | {v['breadth']}/{result['total_authors']} auth{flag} | {v['company'][:30]}")
    print("\n=== TOP 10 BEARISH ===")
    for i, v in enumerate(result["bearish"][:10], 1):
        flag = " CONTESTED" if v["contested"] else ""
        print(f"{i:2}. {v['ticker']:6} {v['tier']} {v['score']:5} | {v['breadth']}/{result['total_authors']} auth{flag} | {v['company'][:30]}")

    if review is not None:
        print(f"\n{review_line(review, result['disputed'])}")
    if integ is None:
        print("\nIntegrity: chunk files not found in --extract-dir, skipped")
    else:
        status = "OK" if integ["ok"] else f"FAIL missing={integ['missing']}"
        print(f"\nIntegrity: corpus N={integ['n']} covered={integ['covered']} "
              f"with_tickers={integ['with_tickers']} missing={integ['missing']} [{status}]")
    print(f"\n✅ Leaderboard → {args.output} (+ {md_path})")
    return 0
