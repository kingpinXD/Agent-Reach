# -*- coding: utf-8 -*-
"""List a channel's recent uploads via yt-dlp's flat playlist view.

The validated command shape prints `id<TAB>upload_date<TAB>title`, newest-first,
without fetching each video page:

    yt-dlp --flat-playlist --extractor-args "youtubetab:approximate_date"
           --lazy-playlist --print "%(id)s\\t%(upload_date)s\\t%(title)s"
           "https://www.youtube.com/channel/<id>/videos"
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Iterator, Mapping
from datetime import date, timedelta

from agent_reach.utils.process import utf8_subprocess_env

_YTDLP_PRINT = "%(id)s\t%(upload_date)s\t%(title)s"


def _channel_url(channel_id: str) -> str:
    return f"https://www.youtube.com/channel/{channel_id}/videos"


def _run_ytdlp_lines(channel_id: str) -> Iterator[str]:
    """Stream raw `id\\tdate\\ttitle` lines from yt-dlp, newest-first.

    Isolated so tests can monkeypatch it with canned lines (no network).
    Yields decoded stdout lines; terminates the process when the caller stops
    iterating (early-stop). Raises CalledProcessError on a nonzero exit with no
    output consumed.
    """
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--extractor-args",
        "youtubetab:approximate_date",
        "--lazy-playlist",
        "--print",
        _YTDLP_PRINT,
        _channel_url(channel_id),
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        env=utf8_subprocess_env(),
    )
    yielded = False
    early_stop = True
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            yielded = True
            yield line.rstrip("\n")
        early_stop = False  # stdout drained on its own — not a caller early-stop
    finally:
        # Caller may stop early (cutoff / cache hit) — kill the still-running
        # process rather than waiting for the whole channel to drain.
        if proc.poll() is None:
            proc.terminate()
        stderr = proc.stderr.read() if proc.stderr else ""
        proc.wait()
        # Only treat a nonzero exit as fatal when yt-dlp truly failed: it ran to
        # completion (no early-stop) and gave us nothing.
        if proc.returncode and not yielded and not early_stop:
            raise subprocess.CalledProcessError(proc.returncode, cmd, stderr=stderr)


def _parse_line(line: str, channel_id: str) -> dict | None:
    """Parse one `id\\tdate\\ttitle` line into a row, or None if unusable."""
    parts = line.split("\t")
    if len(parts) < 2:
        return None
    video_id, upload_date = parts[0].strip(), parts[1].strip()
    title = parts[2] if len(parts) > 2 else ""
    if not video_id or upload_date.upper() in ("", "NA", "NONE"):
        return None
    if len(upload_date) != 8 or not upload_date.isdigit():
        return None
    return {
        "video_id": video_id,
        "upload_date": upload_date,
        "title": title,
        "channel_id": channel_id,
    }


def list_channel_videos(
    channel_id: str,
    *,
    weeks: int,
    cache: Mapping[str, dict] | None = None,
    today: date | None = None,
    _lines: Iterable[str] | None = None,
) -> list[dict]:
    """Recent uploads for a channel within the last `weeks`, newest-first.

    Returns rows {video_id, upload_date (YYYYMMDD), title, channel_id}.

    EARLY STOP: yt-dlp prints newest-first, so we stop reading the moment we hit
    either (a) a video whose upload_date < cutoff, or (b) a video_id already in
    `cache`. Everything beyond that point is older and already known/out-of-window,
    so there is nothing left to gain by reading on.

    Bad lines (missing/`NA` date) are skipped, never fatal. `today` is injectable
    for tests; `_lines` overrides the yt-dlp call for tests.
    """
    today = today or date.today()
    cutoff = today - timedelta(weeks=weeks)
    cutoff_str = cutoff.strftime("%Y%m%d")

    lines = _lines if _lines is not None else _run_ytdlp_lines(channel_id)

    rows: list[dict] = []
    parsed_any = False
    for line in lines:
        row = _parse_line(line, channel_id)
        if row is None:
            continue
        parsed_any = True
        if row["upload_date"] < cutoff_str:
            break
        if cache is not None and row["video_id"] in cache:
            break
        rows.append(row)

    if not parsed_any and _lines is None:
        # yt-dlp produced nothing parseable — surface it instead of silently
        # returning an empty window.
        raise RuntimeError(f"yt-dlp returned no usable rows for channel {channel_id}")

    return rows
