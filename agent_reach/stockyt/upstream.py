# -*- coding: utf-8 -*-
"""The one stockyt module that imports upstream Agent-Reach.

Everything stockyt needs from upstream (the pinned yt-dlp runner, the VTT cleaner, Whisper,
the error codes) is re-exported or wrapped here, so an upstream sync can only break this file.
"""

from __future__ import annotations

from agent_reach.channels.youtube import YouTubeChannel, _run_ytdlp, _ytdlp_argv, vtt_to_text
from agent_reach.runtime.result import (
    NOT_FOUND,
    PLATFORM_BLOCKED,
    RATE_LIMITED,
    TIMEOUT,
    ReachError,
)
from agent_reach.utils.process import utf8_subprocess_env

__all__ = [
    "NOT_FOUND",
    "PLATFORM_BLOCKED",
    "RATE_LIMITED",
    "TIMEOUT",
    "ReachError",
    "run_ytdlp",
    "utf8_subprocess_env",
    "vtt_to_text",
    "whisper_transcribe",
    "ytdlp_argv",
]


def ytdlp_argv(*args: str) -> list[str]:
    """argv that runs the yt-dlp pinned in upstream's dependencies (`python -m yt_dlp`)."""
    return _ytdlp_argv(*args)


def run_ytdlp(args: list[str], timeout: float, cwd: str | None = None) -> str:
    """Run yt-dlp and return stdout; failures raise ReachError with upstream's code mapping."""
    return _run_ytdlp(args, timeout, cwd=cwd)


def whisper_transcribe(url: str, timeout: float) -> str:
    """Speech-to-text of a video's audio through upstream's Whisper providers."""
    return YouTubeChannel()._whisper_transcribe(target=url, timeout=timeout)[0].get("text") or ""
