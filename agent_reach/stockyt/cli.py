# -*- coding: utf-8 -*-
"""`agent-reach stockyt <command>` — the finance-YouTube pipeline commands.

`run` runs the whole pipeline in a run folder (`pipeline.py`); `status`, `regrade` and `doctor`
sit beside it. The single-step commands (channels, transcripts, split-corpus, review-apply,
grade) still work on their own files. Prose output for humans and skills, not the JSON
envelope the rest of agent-reach prints.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from agent_reach.stockyt.pipeline import BACKENDS, EXIT_CODES


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

    p_run = sub.add_parser("run", help="Run the whole pipeline into OUTPUT_DIR/runs/<label>/, or resume a run")
    p_run.add_argument("--since", type=_since_value,
                       help="Window start: a day (YYYYMMDD) or moment (YYYYMMDDTHHMM, in the configured timezone)")
    p_run.add_argument("--until", type=_day_value, default=None, help="Window end, inclusive (YYYYMMDD)")
    p_run.add_argument("--label", help="Name of the new run's folder (letters, digits, '.', '_', '-')")
    p_run.add_argument("--resume", metavar="LABEL", help="Continue this run where it stopped")
    p_run.add_argument("--retry-failed", action="store_true",
                       help="With --resume: ask the LLM tasks that stayed invalid after their re-ask again")
    p_run.add_argument("--llm", choices=BACKENDS, default=None,
                       help="LLM backend (default: the configured llm_backend, claude-cli)")
    p_run.add_argument("--cookies-from-browser", default=None,
                       help="Send this browser's YouTube cookies (only with the user's OK); kept for --resume")

    p_st = sub.add_parser("status", help="Show where a run stands")
    p_st.add_argument("label")

    p_rg = sub.add_parser("regrade", help="Grade a reviewed run again with the current weights and rewrite its report")
    p_rg.add_argument("label")

    p_dr = sub.add_parser("doctor", help="Check yt-dlp, the JS runtime, the output folder, claude -p and the cache")
    p_dr.add_argument("--llm", choices=BACKENDS, default=None, help="Check this backend (default: the configured one)")

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
        "run": _cmd_run,
        "status": _cmd_status,
        "regrade": _cmd_regrade,
        "doctor": _cmd_doctor,
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
    return EXIT_CODES[status]


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
    return EXIT_CODES[status]


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
    from agent_reach.stockyt.stages import review_lines

    counts = review_run(args.run_dir)
    for line in review_lines(counts):
        print(line)
    if not counts["ok"]:
        print("Fix the chunks above and re-run review-apply")
    return 0 if counts["ok"] else 1


def _cmd_grade(args) -> int:
    """Grade extracted (or reviewed) ticker rows into bullish/bearish leaderboards."""
    from agent_reach.stockyt.config import load_settings
    from agent_reach.stockyt.grading import grade
    from agent_reach.stockyt.report import console_lines, leaderboard_markdown, review_line
    from agent_reach.stockyt.review import reviewed_rows
    from agent_reach.stockyt.run import atomic_write_json, atomic_write_text, read_json
    from agent_reach.stockyt.stages import chunks, extract_counts, extract_line

    review = None
    if args.reviewed:
        try:
            extract_rows, review = reviewed_rows(args.extract_dir)
        except ValueError as e:
            print(f"❌ Review: FAIL — {e}", file=sys.stderr)
            return 1
    else:
        extract_rows = []
        for path in sorted(glob.glob(os.path.join(args.extract_dir, "extract_*.jsonl"))):
            extract_rows.extend(_read_jsonl(path))

    authors = read_json(args.authors)
    result = grade(extract_rows, authors["videos_per_author"], authors["total_authors"], load_settings().weights)
    if review is not None:
        result["review"] = review
    counts = extract_counts(args.extract_dir) if chunks(args.extract_dir) else None
    if counts is not None:
        result["integrity"] = counts

    atomic_write_json(args.output, result)
    md_path = os.path.splitext(args.output)[0] + ".md"
    atomic_write_text(md_path, leaderboard_markdown(result))

    print(f"Graded {len(extract_rows)} rows → {len(result['tickers'])} tickers")
    for line in console_lines(result):
        print(line)
    if review is not None:
        print(f"\n{review_line(review, result['disputed'])}")
    print("\nIntegrity: no chunk_*.jsonl in --extract-dir, skipped" if counts is None
          else f"\nIntegrity: {extract_line(counts)}")
    print(f"\n✅ Leaderboard → {args.output} (+ {md_path})")
    return 1 if counts is not None and counts["extracted"] < counts["n"] else 0


def _load_run(label: str):
    from agent_reach.stockyt import run as run_mod
    from agent_reach.stockyt.config import load_settings

    return run_mod.open_run(load_settings().output_dir, label)


def _cmd_run(args) -> int:
    """Start a run (or resume one), check the setup first, then run the stages; exit 0/1/2/3."""
    from agent_reach.stockyt import pipeline
    from agent_reach.stockyt import run as run_mod
    from agent_reach.stockyt.config import load_settings

    if args.resume and (args.since or args.until or args.label):
        print("❌ --resume continues a run with its own window: drop --since, --until and --label", file=sys.stderr)
        return 1
    if not args.resume and not (args.since and args.label):
        print("❌ A new run needs --since and --label (or use --resume <label>)", file=sys.stderr)
        return 1
    if args.retry_failed and not args.resume:
        print("❌ --retry-failed only goes with --resume", file=sys.stderr)
        return 1

    settings = load_settings()
    backend = args.llm or settings.llm_backend
    if not _doctor_ok(settings, backend):
        print("❌ Fix the FAIL lines above first (agent-reach stockyt doctor)", file=sys.stderr)
        return 1

    try:
        changes = {}
        if args.resume:
            run = run_mod.open_run(settings.output_dir, args.resume)
            changes = {key: value for key, value in (("llm", args.llm), ("cookies_from_browser", args.cookies_from_browser))
                       if value}
        else:
            params = {"since": args.since, "until": args.until, "llm": backend,
                      "cookies_from_browser": args.cookies_from_browser or settings.cookies_from_browser}
            run = run_mod.create(settings.output_dir, args.label, params, settings.snapshot())
        print(f"Run {run.label}: {run.path}")
        return pipeline.execute(run, params=changes, retry_failed=args.retry_failed)
    except (FileExistsError, FileNotFoundError, ValueError, run_mod.RunLocked) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1


def _cmd_status(args) -> int:
    """Print each stage's status and gate lines, and the prompt files a waiting run needs answered."""
    from agent_reach.stockyt import pipeline

    try:
        run = _load_run(args.label)
    except (FileNotFoundError, ValueError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    params = run.data["params"]
    print(f"Run {run.label} ({run.path}) · stockyt {run.data['stockyt_version']}")
    print(f"Window: since {params['since']}" + (f" until {params['until']}" if params.get("until") else "")
          + f" · LLM {params['llm']} · cookies {params.get('cookies_from_browser') or 'off'}")
    for stage in pipeline.STAGES:
        record = run.data["stages"].get(stage.name) or {"status": "pending", "attempts": 0, "gate_lines": []}
        print(f"[{stage.name}] {record['status']} · attempts {record['attempts']}")
        for line in record["gate_lines"]:
            print(f"  {line}")
    for prompt in pipeline.pending_prompts(run):
        print(f"waiting: {prompt}")
    return 0


def _cmd_regrade(args) -> int:
    """Re-run grade + report on a reviewed run with the weights in today's config file."""
    from agent_reach.stockyt import pipeline
    from agent_reach.stockyt import run as run_mod
    from agent_reach.stockyt.config import load_settings

    try:
        run = _load_run(args.label)
        if (run.data["stages"].get("review") or {}).get("status") != "done":
            print(f"❌ run {args.label!r} has no finished review yet: resume it first", file=sys.stderr)
            return 1
        return pipeline.execute(run, weights=load_settings().weights, stage_list=pipeline.STAGES[-2:], force=True)
    except (FileNotFoundError, ValueError, run_mod.RunLocked) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1


def _doctor_ok(settings, backend: str) -> bool:
    """Print the setup checks; False when any of them failed."""
    from agent_reach.stockyt import pipeline

    checks = pipeline.doctor(settings, backend)
    for line in pipeline.doctor_lines(checks):
        print(line)
    return not any(ok is False for ok, _ in checks)


def _cmd_doctor(args) -> int:
    from agent_reach.stockyt.config import load_settings

    settings = load_settings()
    return 0 if _doctor_ok(settings, args.llm or settings.llm_backend) else 1
