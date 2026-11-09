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
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import date

from agent_reach.channels.youtube import _ytdlp_argv
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
    cmd = _ytdlp_argv(
        "--flat-playlist",
        "--extractor-args",
        "youtubetab:approximate_date",
        "--lazy-playlist",
        "--print",
        _YTDLP_PRINT,
        _channel_url(channel_id),
    )
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


def _real_upload_date(video_id: str) -> str | None:
    """The video's own upload_date (YYYYMMDD) from its watch page, or None if it can't be read."""
    cmd = _ytdlp_argv("--skip-download", "--print", "%(upload_date)s",
                      f"https://www.youtube.com/watch?v={video_id}")
    try:
        out = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                             env=utf8_subprocess_env(), timeout=120).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out if len(out) == 8 and out.isdigit() else None


def list_channel_videos(
    channel_id: str,
    *,
    cutoff: date,
    cache: Mapping[str, dict] | None = None,
    _lines: Iterable[str] | None = None,
    _real_date: Callable[[str], str | None] = _real_upload_date,
) -> list[dict]:
    """Uploads for a channel on or after `cutoff`, newest-first.

    Returns rows {video_id, upload_date (YYYYMMDD), title, channel_id}.

    EARLY STOP: yt-dlp prints newest-first, so we always stop reading once we hit
    a video whose upload_date < cutoff — everything beyond is out-of-window. As an
    optimization we also stop on a video_id already in `cache`, but ONLY when the
    cache is known to already reach back to the cutoff (its oldest cached upload_date
    <= cutoff). Otherwise a grown window (cache seeded from a smaller window) would
    stop at the first cached id and never back-fill the older in-window videos.

    CUTOFF DAY: the flat listing's approximate date is the real upload date or one
    day later, never earlier (checked against per-video dates, 2026-11-08). So a row
    dated after the cutoff is in the window and a row dated before it is out, but a
    row dated exactly on the cutoff may really be from the day before. Those rows get
    their real date from `_real_date`; one that can't be read is kept and marked
    `date_verified: False`.

    Bad lines (missing/`NA` date) are skipped, never fatal. `_lines` and `_real_date`
    override the yt-dlp calls for tests.
    """
    cutoff_str = cutoff.strftime("%Y%m%d")

    lines = _lines if _lines is not None else _run_ytdlp_lines(channel_id)

    # Oldest upload_date the cache already covers. The cached-id early-stop is only
    # safe once the cache reaches the cutoff (cache_floor <= cutoff_str); a newer
    # floor means the requested window grew and older videos still need back-filling.
    cache_floor = (
        min(row["upload_date"] for row in cache.values()) if cache else None
    )
    cache_reaches_cutoff = cache_floor is not None and cache_floor <= cutoff_str

    rows: list[dict] = []
    parsed_any = False
    for line in lines:
        row = _parse_line(line, channel_id)
        if row is None:
            continue
        parsed_any = True
        if row["upload_date"] < cutoff_str:
            break
        if cache_reaches_cutoff and row["video_id"] in cache:
            break
        if row["upload_date"] == cutoff_str:
            real = _real_date(row["video_id"])
            if real is None:
                row["date_verified"] = False
            elif real < cutoff_str:
                continue
            else:
                row["upload_date"] = real
        rows.append(row)

    if not parsed_any and _lines is None:
        # yt-dlp produced nothing parseable — surface it instead of silently
        # returning an empty window.
        raise RuntimeError(f"yt-dlp returned no usable rows for channel {channel_id}")

    return rows
