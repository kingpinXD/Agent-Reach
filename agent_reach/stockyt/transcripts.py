# -*- coding: utf-8 -*-
"""One transcript record per YouTube link, built on the youtube channel's yt-dlp backends.

Record shape: {url, video_id, channel, channel_id, transcript, source, status[, error]}.
`source` is "subtitles" (manual or auto captions, fetched in one call) or "whisper".
A failing link becomes a status:"error" record; nothing here raises.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from agent_reach.channels.youtube import YouTubeChannel

DEFAULT_TIMEOUT = 120.0


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
        vid = parse_qs(parsed.query).get("v", [None])[0]
        return vid or None
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


def _metadata(youtube: YouTubeChannel, url: str, timeout: float) -> tuple[str | None, str | None]:
    """(channel name, channel id), or (None, None) — a miss never changes the record's status."""
    try:
        item = youtube._ytdlp_read(target=url, timeout=timeout)[0]
    except Exception:
        return None, None
    return item.get("author"), (item.get("raw") or {}).get("channel_id")


def _transcript(youtube: YouTubeChannel, url: str, lang: str, allow_whisper: bool, timeout: float) -> tuple[str, str]:
    """(text, source): captions first, then Whisper when allowed. Raises on total failure."""
    try:
        text = youtube._ytdlp_subtitle(target=url, lang=lang, timeout=timeout)[0].get("text")
        caption_error = "no transcript available"
    except Exception as e:
        if not allow_whisper:
            raise
        text, caption_error = None, str(e)
    if text:
        return " ".join(text.splitlines()), "subtitles"
    if not allow_whisper:
        raise RuntimeError(caption_error)
    try:
        text = youtube._whisper_transcribe(target=url, timeout=timeout)[0].get("text")
    except Exception as e:
        # Report both: the caption error is usually the real cause (e.g. a YouTube rate limit).
        raise RuntimeError(f"captions: {caption_error}; whisper: {e}") from None
    if not text:
        raise RuntimeError(f"captions: {caption_error}; whisper: no text")
    return text, "whisper"


def fetch_record(url: str, *, lang: str = "en", allow_whisper: bool = True, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Build one transcript record for `url`. Any failure → status "error" with the message."""
    youtube = YouTubeChannel()
    channel, channel_id = _metadata(youtube, url, timeout)
    record = {"url": url, "video_id": extract_video_id(url), "channel": channel, "channel_id": channel_id}
    try:
        transcript, source = _transcript(youtube, url, lang, allow_whisper, timeout)
    except Exception as e:
        record.update({"transcript": None, "source": None, "status": "error", "error": str(e)})
    else:
        record.update({"transcript": transcript, "source": source, "status": "ok"})
    return record
