# -*- coding: utf-8 -*-
"""The transcripts stage: one caption row per video, fetched one at a time with a pause.

Row shape: {url, video_id, channel, channel_id, upload_date, timestamp, transcript, source,
lang, cookies, status, error, error_kind, attempts}. `source` is "subtitles" or "whisper";
`error_kind` is rate_limited | no_captions | unavailable | other. A failing video becomes a
status:"error" row; the stage never raises on one video.

Resumable: rows are appended as they arrive, a re-run skips videos already `ok`, and the file
is rewritten with one row per video at the end.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

from agent_reach.stockyt import youtube
from agent_reach.stockyt.config import Settings
from agent_reach.stockyt.run import LogCall, atomic_write_jsonl


def extract_video_id(url: str) -> str | None:
    """Pull a YouTube video id out of a URL; return None if unparseable."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "youtu.be" in host:
        vid = parsed.path.strip("/").split("/")[0]
        return vid or None
    if "youtube.com" in host:
        if parsed.path.startswith("/shorts/"):
            vid = parsed.path.split("/shorts/", 1)[1].split("/")[0]
            return vid or None
        return parse_qs(parsed.query).get("v", [None])[0] or None
    return None


def read_link_file(path: str) -> list[str]:
    """Read a links file: one URL per line, skipping blanks and # comments."""
    links = []
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        links.append(line)
    return links


def _link_key(url: str) -> str:
    return extract_video_id(url) or url


def _row_key(row: dict) -> str:
    return row.get("video_id") or row["url"]


def dedupe_links(links: list[str]) -> list[str]:
    """Links in order, keeping the first link per video id."""
    seen: dict[str, str] = {}
    for url in links:
        seen.setdefault(_link_key(url), url)
    return list(seen.values())


def read_rows(path: str) -> list[dict]:
    """The latest row per video in a transcripts JSONL (later lines win); [] if there is no file."""
    if not os.path.exists(path):
        return []
    latest: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                latest.pop(_row_key(row), None)
                latest[_row_key(row)] = row
    return list(latest.values())


def _whisper(row: dict, caption_error: Exception) -> None:
    try:
        text = youtube.whisper_text(row["url"])
    except Exception as e:
        # Report both: the caption error is usually the real cause.
        row.update(error=f"captions: {caption_error}; whisper: {e}", error_kind="no_captions")
        return
    if not text:
        row.update(error=f"captions: {caption_error}; whisper: no text", error_kind="no_captions")
        return
    row.update(transcript=text, source="whisper", status="ok", error=None, error_kind=None)


def fetch_row(url: str, settings: Settings, attempts: int = 1) -> dict:
    """One video's transcript row; any failure becomes status "error" with its kind."""
    video_id = extract_video_id(url)
    row = {
        "url": url, "video_id": video_id, "channel": None, "channel_id": None,
        "upload_date": None, "timestamp": None, "transcript": None, "source": None, "lang": None,
        "cookies": bool(settings.cookies_from_browser), "status": "error", "error": None,
        "error_kind": None, "attempts": attempts,
    }
    if video_id is None:
        row.update(error="not a YouTube video link", error_kind="other")
        return row
    try:
        captions = youtube.fetch_captions(youtube.watch_url(video_id), youtube.caption_langs(settings.lang),
                                          settings.cookies_from_browser)
    except Exception as e:
        row.update(error=str(e), error_kind=youtube.error_kind(e))
        if row["error_kind"] == "no_captions" and settings.whisper:
            _whisper(row, e)
        return row
    row.update(
        channel=settings.channel_name(captions["channel_id"]) or captions["channel"],
        channel_id=captions["channel_id"],
        upload_date=captions["upload_date"],
        timestamp=captions["timestamp"],
        transcript=captions["text"],
        source="subtitles",
        lang=captions["lang"],
        status="ok",
    )
    return row


def fetch_all(
    links: list[str],
    out_path: str,
    settings: Settings,
    *,
    sleep: Callable[[float], None] = time.sleep,
    log: LogCall | None = None,
) -> list[dict]:
    """Fetch every link's transcript into `out_path`, one video at a time, and return the rows.

    Videos already `ok` in `out_path` are skipped. The first rate-limited video stops the loop:
    more calls only extend YouTube's block, so the rest stay missing until a resume.
    """
    links = dedupe_links(links)
    rows = {_row_key(row): row for row in read_rows(out_path)}
    wait = youtube.pacer(settings.pace_seconds, sleep)
    with open(out_path, "a", encoding="utf-8") as out:
        for url in links:
            key = _link_key(url)
            prior = rows.get(key)
            if prior and prior["status"] == "ok":
                continue
            wait()
            started = time.monotonic()
            row = fetch_row(url, settings, attempts=(prior or {}).get("attempts", 0) + 1)
            if log:
                log("captions", key, time.monotonic() - started, row["error_kind"] or row["status"])
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            rows[key] = row
            if row["error_kind"] == "rate_limited":
                break

    link_keys = [_link_key(url) for url in links]
    final = [rows.pop(k) for k in link_keys if k in rows] + list(rows.values())  # rows of other links last
    atomic_write_jsonl(out_path, final)
    return final


def reconcile(links: list[str], rows: list[dict]) -> dict:
    """Counts of the links' videos: {n, ok, error, rate_limited, missing}. rate_limited is part of error."""
    keys = [_link_key(url) for url in dedupe_links(links)]
    latest = {_row_key(row): row for row in rows}
    found = [latest[k] for k in keys if k in latest]
    ok = sum(row["status"] == "ok" for row in found)
    return {
        "n": len(keys),
        "ok": ok,
        "error": len(found) - ok,
        "rate_limited": sum(row["status"] != "ok" and row.get("error_kind") == "rate_limited" for row in found),
        "missing": len(keys) - len(found),
    }


def status(counts: dict) -> str:
    """Stage status: blocked on a rate limit (cookies need the user's OK), failed on missing rows."""
    if counts["rate_limited"]:
        return "blocked"
    if counts["missing"]:
        return "failed"
    return "done"


def check(links: list[str], rows: list[dict]) -> tuple[str, list[str]]:
    """(stage status, gate lines) for the transcripts stage."""
    counts = reconcile(links, rows)
    st = status(counts)
    lines = [
        f"Transcripts: {counts['n']} links · {counts['ok']} ok · {counts['error']} error "
        f"({counts['rate_limited']} rate_limited) · {counts['missing']} missing [{'OK' if st == 'done' else 'FAIL'}]"
    ]
    if st == "blocked":
        lines.append("YouTube is rate limiting caption calls: re-run with --cookies-from-browser <browser> "
                     "after the user agrees")
    elif st == "failed":
        lines.append("Some videos have no row yet: re-run to fetch them")
    return st, lines
