# -*- coding: utf-8 -*-
"""Every yt-dlp call stockyt makes: captions, a video's upload time, and a channel's listing.

Two module-level hooks run yt-dlp: `run_ytdlp` (one call, returns stdout) and `stream_ytdlp`
(yields stdout lines, so a listing can stop early). Tests swap both, usually for `replay()`,
which answers from recorded yt-dlp output instead of the network.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping

from agent_reach.stockyt import upstream
from agent_reach.stockyt.upstream import NOT_FOUND, PLATFORM_BLOCKED, RATE_LIMITED, ReachError

DEFAULT_TIMEOUT = 120.0
NO_CAPTIONS = "NO_CAPTIONS"

_CAPTION_PRINT = "%(channel)s\t%(channel_id)s\t%(upload_date)s\t%(timestamp)s"
_TIME_PRINT = "%(upload_date)s\t%(timestamp)s"
_LISTING_PRINT = "%(id)s\t%(upload_date)s\t%(title)s"

# A sign-in or bot challenge clears the same way a 429 does: cookies, with the user's OK.
_ERROR_KINDS = {
    RATE_LIMITED: "rate_limited",
    PLATFORM_BLOCKED: "rate_limited",
    NO_CAPTIONS: "no_captions",
    NOT_FOUND: "unavailable",
}

Runner = Callable[[list[str], float, "str | None"], str]
LineStream = Callable[[list[str]], Iterator[str]]


def _stream_lines(args: list[str]) -> Iterator[str]:
    """Yield yt-dlp's stdout lines; stopping early terminates the process."""
    cmd = upstream.ytdlp_argv(*args)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                            errors="replace", env=upstream.utf8_subprocess_env())
    drained = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line.rstrip("\n")
        drained = True
    finally:
        if proc.poll() is None:
            proc.terminate()
        stderr = proc.stderr.read() if proc.stderr else ""
        proc.wait()
        if drained and proc.returncode:
            last = next((ln for ln in reversed(stderr.splitlines()) if ln.strip()), "no error output")
            raise RuntimeError(f"yt-dlp exited {proc.returncode}: {last}")


run_ytdlp: Runner = upstream.run_ytdlp
stream_ytdlp: LineStream = _stream_lines


def replay(recordings: Mapping[str, dict]) -> tuple[Runner, LineStream]:
    """A runner and a line stream that answer from recorded yt-dlp output, keyed by the URL argument.

    A recording is {"stdout": str, "files": {name: text}} — files are written into the call's
    working folder, as yt-dlp writes caption files — or {"error": <ReachError code>, "message": str}.
    """

    def recording(args: list[str]) -> dict:
        url = args[-1]
        if url not in recordings:
            raise KeyError(f"no recorded yt-dlp output for {url}")
        rec = recordings[url]
        if "error" in rec:
            raise ReachError(rec["error"], rec.get("message", rec["error"]))
        return rec

    def run(args: list[str], timeout: float, cwd: str | None = None) -> str:
        rec = recording(args)
        for name, text in rec.get("files", {}).items():
            with open(os.path.join(cwd or ".", name), "w", encoding="utf-8") as f:
                f.write(text)
        return rec.get("stdout", "")

    def stream(args: list[str]) -> Iterator[str]:
        yield from recording(args).get("stdout", "").splitlines()

    return run, stream


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def channel_url(channel_id: str) -> str:
    return f"https://www.youtube.com/channel/{channel_id}/videos"


def caption_langs(lang: str) -> tuple[str, str]:
    """Caption tracks to ask for, best first: the original-language track, then the plain one."""
    return f"{lang}-orig", lang


def _field(value: str) -> str | None:
    value = value.strip()
    return None if value.upper() in ("", "NA", "NONE") else value


def _timestamp(value: str) -> int | None:
    value = _field(value) or ""
    return int(value) if value.isdigit() else None


def _upload_date(value: str) -> str | None:
    value = _field(value) or ""
    return value if len(value) == 8 and value.isdigit() else None


def _sub_lang(filename: str) -> str:
    """`<id>.<lang>.vtt` → `<lang>`."""
    return filename.rsplit(".", 2)[-2]


def fetch_captions(
    url: str,
    langs: tuple[str, ...] = ("en-orig", "en"),
    cookies_from_browser: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """One yt-dlp call: the video's captions plus its channel and upload time.

    Returns {text, lang, channel, channel_id, upload_date, timestamp}. `lang` is the first of
    `langs` that has a track. Raises ReachError: NO_CAPTIONS when none of `langs` exists, or
    upstream's code (RATE_LIMITED, NOT_FOUND, ...) when yt-dlp fails.
    """
    args = [
        "--skip-download", "--no-simulate",
        "--write-subs", "--write-auto-subs",
        "--sub-langs", ",".join(langs), "--sub-format", "vtt",
        "--print", _CAPTION_PRINT,
        "-o", "%(id)s.%(ext)s",
    ]
    if cookies_from_browser:
        args += ["--cookies-from-browser", cookies_from_browser]
    with tempfile.TemporaryDirectory(prefix="stockyt-subs-") as tmp:
        out = run_ytdlp([*args, url], timeout, tmp)
        files = {_sub_lang(name): name for name in os.listdir(tmp) if name.endswith(".vtt")}
        lang = next((code for code in langs if code in files), None)
        if lang is None:
            raise ReachError(NO_CAPTIONS, f"no captions in {', '.join(langs)} for this video")
        with open(os.path.join(tmp, files[lang]), encoding="utf-8", errors="replace") as f:
            text = upstream.vtt_to_text(f.read())

    printed = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
    channel, channel_id, upload_date, timestamp = (printed.split("\t") + ["", "", "", ""])[:4]
    return {
        "text": " ".join(text.splitlines()),
        "lang": lang,
        "channel": _field(channel),
        "channel_id": _field(channel_id),
        "upload_date": _upload_date(upload_date),
        "timestamp": _timestamp(timestamp),
    }


def error_kind(exc: BaseException) -> str:
    """rate_limited | no_captions | unavailable | other, from the ReachError code."""
    if not isinstance(exc, ReachError):
        return "other"
    return _ERROR_KINDS.get(exc.code, "other")


def video_time(video_id: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[str | None, int | None]:
    """The video's own (upload_date YYYYMMDD, unix timestamp); (None, None) when yt-dlp fails."""
    try:
        out = run_ytdlp(["--skip-download", "--print", _TIME_PRINT, watch_url(video_id)], timeout, None)
    except ReachError:
        return None, None
    upload_date, timestamp = (out.strip().split("\t") + ["", ""])[:2]
    return _upload_date(upload_date), _timestamp(timestamp)


def list_channel(channel_id: str) -> Iterator[str]:
    """`id<TAB>upload_date<TAB>title` lines of a channel's uploads, newest first, one page at a time.

    The flat listing's `approximate_date` is the real upload date or one day late.
    """
    return stream_ytdlp([
        "--flat-playlist",
        "--extractor-args", "youtubetab:approximate_date",
        "--lazy-playlist",
        "--print", _LISTING_PRINT,
        channel_url(channel_id),
    ])


def whisper_text(url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Speech-to-text of the video's audio (needs a Whisper API key)."""
    return upstream.whisper_transcribe(url, timeout)


def pacer(seconds: float, sleep: Callable[[float], None] = time.sleep) -> Callable[[], None]:
    """A wait() to call before each yt-dlp call: it pauses `seconds` before every call but the first."""
    called = False

    def wait() -> None:
        nonlocal called
        if called and seconds > 0:
            sleep(seconds)
        called = True

    return wait
