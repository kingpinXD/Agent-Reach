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

from agent_reach.stockyt.cache import DEFAULT_CACHE_DIR


def _since_date(value: str) -> date:
    """argparse type for --since: a real, non-future YYYYMMDD day."""
    try:
        if len(value) != 8 or not value.isdigit():
            raise ValueError
        day = datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYYMMDD date: {value!r}") from None
    if day > date.today():
        raise argparse.ArgumentTypeError(f"{value} is in the future")
    return day


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-reach stockyt",
                                     description="Finance-YouTube pipeline (deterministic steps)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ch = sub.add_parser("channels", help="List finance-channel uploads by recency into a links file")
    window = p_ch.add_mutually_exclusive_group(required=True)
    window.add_argument("--weeks", type=int, help="Keep videos uploaded within the last N weeks")
    window.add_argument("--since", type=_since_date, help="Keep videos uploaded on or after this day (YYYYMMDD)")
    p_ch.add_argument("-o", "--output", default=None, help="Write watch URLs here, one per line")
    p_ch.add_argument("--cache", default=None, help=f"Cache directory (default: {DEFAULT_CACHE_DIR})")
    p_ch.add_argument("--channels-json", default=None, help="Also write a structured per-channel JSON here")
    p_ch.add_argument("--no-cache", action="store_true", help="Skip the cache: always fetch the full window")

    p_tr = sub.add_parser("transcripts", help="Batch-fetch YouTube transcripts to JSONL (captions → Whisper)")
    p_tr.add_argument("url", nargs="?", default=None, help="A single YouTube URL")
    p_tr.add_argument("--file", default=None,
                      help="File of YouTube URLs, one per line (# comments and blank lines skipped)")
    p_tr.add_argument("-o", "--output", default=None, help="Write JSONL here instead of stdout")
    p_tr.add_argument("--lang", default="en", help="Subtitle language (default: en)")
    p_tr.add_argument("--no-whisper", action="store_true", help="Disable the Whisper fallback (captions only)")

    p_sc = sub.add_parser("split-corpus", help="Split a transcripts JSONL into authors.json + chunk_*.jsonl")
    p_sc.add_argument("--input", required=True, help="transcripts.jsonl from the transcripts command")
    p_sc.add_argument("--out-dir", required=True, help="Directory for authors.json + chunk_*.jsonl")
    p_sc.add_argument("--chunks", type=int, default=30, help="Max contiguous chunks (default: 30)")

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
    """List each tracked finance channel's recent uploads into a links file."""
    from agent_reach.stockyt import cache as cache_mod
    from agent_reach.stockyt import channels as channels_mod
    from agent_reach.stockyt import config as stockyt_config

    cache_dir = args.cache or DEFAULT_CACHE_DIR
    cutoff = args.since or date.today() - timedelta(weeks=args.weeks)
    cutoff_str = cutoff.strftime("%Y%m%d")

    all_urls = []
    json_channels = []
    total = 0
    failed = 0
    for name, channel_id in stockyt_config.CHANNELS:
        existing = None if args.no_cache else cache_mod.load(channel_id, cache_dir)
        try:
            fresh = channels_mod.list_channel_videos(channel_id, cutoff=cutoff, cache=existing)
            if not args.no_cache:
                cache_mod.merge(channel_id, fresh, cache_dir)
                window = cache_mod.all_in_window(channel_id, cutoff_str, cache_dir)
            else:
                window = sorted(fresh, key=lambda r: r["upload_date"], reverse=True)
        except Exception as e:
            # One channel's transient yt-dlp failure must not abort the sweep.
            # Fall back to whatever is already cached for its window.
            failed += 1
            window = [] if args.no_cache else cache_mod.all_in_window(channel_id, cutoff_str, cache_dir)
            print(f"  ⚠️ {name}: fetch failed — {e} (kept {len(window)} cached videos in window)")
        else:
            print(f"  {name}: {len(window)} videos, oldest {window[-1]['upload_date'] if window else '—'}")

        total += len(window)
        all_urls.extend(f"https://www.youtube.com/watch?v={r['video_id']}" for r in window)
        json_channels.append({
            "channel": name,
            "channel_id": channel_id,
            "count": len(window),
            "oldest_date": window[-1]["upload_date"] if window else None,
            "videos": window,
        })

    print(f"Total: {total} videos across {len(stockyt_config.CHANNELS)} channels"
          + (f" ({failed} failed this run)" if failed else ""))
    integrity = sum(c["count"] for c in json_channels)
    print(f"Integrity: {len(all_urls)} URLs == {integrity} summed per-channel counts "
          f"({'ok' if len(all_urls) == integrity else 'MISMATCH'})")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write("\n".join(all_urls) + ("\n" if all_urls else ""))
        print(f"✅ {len(all_urls)} URLs → {args.output}")

    if args.channels_json:
        with open(args.channels_json, "w", encoding="utf-8") as f:
            json.dump(json_channels, f, ensure_ascii=False, indent=2)
        print(f"✅ Structured channels → {args.channels_json}")
    return 0


def _cmd_transcripts(args) -> int:
    """Batch-fetch YouTube transcripts and emit one JSON record per link."""
    from agent_reach.stockyt.transcripts import fetch_record, read_link_file

    if bool(args.url) == bool(args.file):
        print("❌ Provide exactly one of a URL argument or --file", file=sys.stderr)
        return 1

    links = read_link_file(args.file) if args.file else [args.url]

    out = open(args.output, "w", encoding="utf-8") if args.output else None
    ok = 0
    errors = 0
    try:
        for url in links:
            record = fetch_record(url, lang=args.lang, allow_whisper=not args.no_whisper)
            if record["status"] == "ok":
                ok += 1
            else:
                errors += 1
            line = json.dumps(record, ensure_ascii=False)
            if out:
                out.write(line + "\n")
                out.flush()  # append-as-you-go so partial progress survives a crash
            else:
                print(line)
    finally:
        if out:
            out.close()

    dest = args.output if args.output else "stdout"
    print(f"✅ {ok} ok / {errors} errors → {dest}", file=sys.stderr)
    return 0


def _cmd_split_corpus(args) -> int:
    """Split a transcripts JSONL into authors.json + contiguous chunk files."""
    from agent_reach.stockyt.corpus import split_corpus

    authors, num_chunks = split_corpus(_read_jsonl(args.input), args.out_dir, chunks=args.chunks)

    n = authors["N"]
    print(f"Kept {n} ok records → {args.out_dir}")
    print(f"Authors: {authors['total_authors']}")
    for channel, count in sorted(authors["videos_per_author"].items(), key=lambda kv: -kv[1]):
        print(f"  {channel}: {count}")
    chunk_size = -(-n // num_chunks) if num_chunks else 0  # ceil, for display
    print(f"Wrote {num_chunks} chunks (~{chunk_size} records each) + authors.json")
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
