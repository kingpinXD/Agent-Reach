# -*- coding: utf-8 -*-
"""The channels stage: each tracked channel's uploads inside the window, from yt-dlp's flat listing.

The listing (youtube.list_channel) is newest first and its date is the real upload date or one day
late, never earlier. So the listing is read until the first row dated before the window start,
and only rows near an edge of the window cost a per-video time check (youtube.video_time).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from agent_reach.stockyt import cache as cache_mod
from agent_reach.stockyt import youtube

LogCall = Callable[[str, str, float, str], None]


@dataclass(frozen=True)
class Cutoff:
    """Window start: a day, or a moment (`at`, timezone-aware) when --since had a time of day."""

    day: date
    at: datetime | None = None

    @property
    def day_str(self) -> str:
        return self.day.strftime("%Y%m%d")

    def check_days(self) -> set[str]:
        """Listing dates whose rows need their real time: the start day, plus the next day for a time cutoff
        (a row listed a day late may be from before the cutoff time)."""
        days = {self.day}
        if self.at is not None:
            days.add(self.day + timedelta(days=1))
        return {d.strftime("%Y%m%d") for d in days}


def parse_since(value: str, timezone: str) -> Cutoff:
    """`YYYYMMDD` or `YYYYMMDDTHHMM` (read in `timezone`) → Cutoff. Raises ValueError otherwise."""
    error = ValueError(f"not a YYYYMMDD or YYYYMMDDTHHMM value: {value!r}")
    try:
        if len(value) == 8 and value.isdigit():
            return Cutoff(datetime.strptime(value, "%Y%m%d").date())
        if len(value) == 13 and value[8] == "T" and (value[:8] + value[9:]).isdigit():
            at = datetime.strptime(value, "%Y%m%dT%H%M").replace(tzinfo=ZoneInfo(timezone))
            return Cutoff(at.date(), at)
    except ValueError:
        raise error from None
    raise error


@dataclass
class Listing:
    rows: list[dict]
    reached_cutoff: bool  # the listing reached a row older than the window start


@dataclass
class ChannelResult:
    name: str
    channel_id: str
    rows: list[dict]  # the window, newest first
    reached_cutoff: bool
    error: str | None = None  # the fetch failed and `rows` came from the cache


def _parse_line(line: str, channel_id: str) -> dict | None:
    """Parse one `id\\tdate\\ttitle` line into a row, or None if unusable."""
    parts = line.split("\t")
    if len(parts) < 2:
        return None
    video_id, upload_date = parts[0].strip(), parts[1].strip()
    title = parts[2] if len(parts) > 2 else ""
    if not video_id or len(upload_date) != 8 or not upload_date.isdigit():
        return None
    return {
        "video_id": video_id,
        "upload_date": upload_date,
        "title": title,
        "channel_id": channel_id,
        "date_source": "listing",
    }


def _next_day(yyyymmdd: str) -> str:
    return (datetime.strptime(yyyymmdd, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")


def _in_window(row: dict, cutoff: Cutoff, until: str | None,
               video_time: Callable[[str], tuple[str | None, int | None]]) -> bool:
    """Whether a row listed on or after the cutoff day is in the window; looks up its real time near an edge.

    A row whose real time can't be read is kept and marked `date_verified: False`.
    """
    listed = row["upload_date"]
    day_after_until = _next_day(until) if until else None
    if day_after_until and listed > day_after_until:
        return False
    if listed not in cutoff.check_days() and listed != day_after_until:
        return True

    upload_date, timestamp = video_time(row["video_id"])
    if upload_date is None:
        row["date_verified"] = False
        return True
    row.update(upload_date=upload_date, timestamp=timestamp, date_source="video")
    if until and upload_date > until:
        return False
    if cutoff.at is None:
        return upload_date >= cutoff.day_str
    if timestamp is None:
        row["date_verified"] = False
        return upload_date >= cutoff.day_str
    return timestamp >= cutoff.at.timestamp()


def list_channel_videos(
    channel_id: str,
    *,
    cutoff: Cutoff,
    until: str | None = None,
    lines: Iterable[str] | None = None,
    video_time: Callable[[str], tuple[str | None, int | None]] | None = None,
) -> Listing:
    """Uploads from `cutoff` to `until` (YYYYMMDD, inclusive, by real date), newest first.

    Rows: {video_id, upload_date, title, channel_id, date_source[, timestamp][, date_verified]}.
    Bad lines (missing/`NA` date) are skipped. Raises RuntimeError when no line parses.
    """
    lines = youtube.list_channel(channel_id) if lines is None else lines
    video_time = video_time or youtube.video_time
    rows: list[dict] = []
    parsed_any = False
    for line in lines:
        row = _parse_line(line, channel_id)
        if row is None:
            continue
        parsed_any = True
        if row["upload_date"] < cutoff.day_str:
            return Listing(rows, reached_cutoff=True)
        if _in_window(row, cutoff, until, video_time):
            rows.append(row)
    if not parsed_any:
        raise RuntimeError(f"yt-dlp returned no usable rows for channel {channel_id}")
    return Listing(rows, reached_cutoff=False)


def _newest_first(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: r["upload_date"], reverse=True)


def sweep(
    channels: list[tuple[str, str]],
    cutoff: Cutoff,
    *,
    until: str | None = None,
    cache_dir: str | None = None,
    pace_seconds: float = 0,
    sleep: Callable[[float], None] = time.sleep,
    log: LogCall | None = None,
) -> list[ChannelResult]:
    """List every channel's window. One failing channel never stops the sweep: its window falls back
    to the cache (when `cache_dir` is given) and its result carries the error."""
    wait = youtube.pacer(pace_seconds, sleep)

    def logged(kind: str, item_id: str, call: Callable, outcome: Callable):
        wait()
        started = time.monotonic()
        result = call()
        if log:
            log(kind, item_id, time.monotonic() - started, outcome(result))
        return result

    def video_time(video_id: str):
        return logged("video_time", video_id, lambda: youtube.video_time(video_id),
                      lambda r: "ok" if r[0] else "failed")

    results = []
    for name, channel_id in channels:
        try:
            listing = logged("listing", channel_id,
                             lambda: list_channel_videos(channel_id, cutoff=cutoff, until=until, video_time=video_time),
                             lambda r: "ok")
        except Exception as e:
            if log:
                log("listing", channel_id, 0.0, "failed")
            rows = cache_mod.all_in_window(channel_id, cutoff.day_str, cache_dir, until) if cache_dir else []
            results.append(ChannelResult(name, channel_id, rows, reached_cutoff=False, error=str(e)))
            continue
        if cache_dir:
            cache_mod.merge(channel_id, listing.rows, cache_dir)
        results.append(ChannelResult(name, channel_id, _newest_first(listing.rows), listing.reached_cutoff))
    return results


def window_urls(results: list[ChannelResult]) -> list[str]:
    return [youtube.watch_url(row["video_id"]) for r in results for row in r.rows]


def check(results: list[ChannelResult]) -> tuple[str, list[str]]:
    """(stage status, gate lines): failed when a channel's fetch failed or its listing ended before the window."""
    problems = []
    for r in results:
        if r.error:
            problems.append(f"❌ {r.name}: fetch failed — {r.error} (kept {len(r.rows)} cached videos in window)")
        elif not r.reached_cutoff:
            problems.append(f"❌ {r.name}: listing ended before the window start")
    total = sum(len(r.rows) for r in results)
    summary = (f"Channels: {len(results)} channels · {total} videos · {len(problems)} failed "
               f"[{'FAIL' if problems else 'OK'}]")
    return ("failed" if problems else "done"), [summary, *problems]
