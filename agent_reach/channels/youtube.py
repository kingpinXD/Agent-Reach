# -*- coding: utf-8 -*-
"""YouTube — check if yt-dlp is available with JS runtime."""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from agent_reach.probe import probe_command
from agent_reach.utils.paths import get_ytdlp_config_path, render_ytdlp_fix_command
from agent_reach.utils.process import utf8_subprocess_env
from agent_reach.utils.text import read_utf8_text

from .base import Channel

# Inline cue-timestamp (<00:00:01.234>) and <c>...</c> styling yt-dlp emits in auto-subs.
_VTT_INLINE_TAG = re.compile(r"<[^>]+>")


def _has_js_runtime_config(config_path) -> bool:
    """Return whether yt-dlp config explicitly enables a JS runtime."""
    try:
        if not config_path.exists():
            return False
        return "--js-runtimes" in read_utf8_text(config_path)
    except OSError:
        return False


def _clean_vtt(vtt_text: str) -> str:
    """Reduce raw VTT to a single space-separated paragraph of spoken text.

    Strips the WEBVTT header, metadata lines, cue-timing lines, inline timing
    and styling tags, then collapses the consecutive duplicate lines that
    YouTube auto-captions emit (each cue repeats the previous line as it scrolls).
    """
    lines = []
    for raw in vtt_text.splitlines():
        line = _VTT_INLINE_TAG.sub("", raw).strip()
        if not line or "-->" in line:
            continue
        if line == "WEBVTT" or line.startswith(("Kind:", "Language:")):
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return " ".join(lines)


def _download_subtitles(url: str, lang: str, tmpdir: str, auto: bool) -> str | None:
    """Fetch one subtitle track via yt-dlp; return raw VTT text or None.

    Never raises on a yt-dlp failure — a missing track or a dead link returns
    None so the caller can fall through to the next source.
    """
    sub_flag = "--write-auto-sub" if auto else "--write-sub"
    cmd = [
        "yt-dlp",
        "--skip-download",
        sub_flag,
        "--sub-format",
        "vtt",
        "--sub-lang",
        lang,
        "-o",
        str(Path(tmpdir) / "%(id)s"),
        url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env=utf8_subprocess_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None

    files = sorted(Path(tmpdir).glob(f"*.{lang}.vtt"))
    if not files:
        return None
    return read_utf8_text(files[0]) or None


def _fetch_metadata(url: str) -> tuple[str | None, str | None]:
    """Return (channel_name, channel_id) via one lightweight yt-dlp print call.

    No media or subtitle download. Falls back to the uploader name when the
    channel field is empty. Never raises — any failure yields (None, None) so a
    missing channel can't change a link's transcript status.
    """
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--no-warnings",
        "--print",
        "%(channel)s\t%(channel_id)s\t%(uploader)s",
        url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            env=utf8_subprocess_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    if proc.returncode != 0:
        return None, None

    parts = proc.stdout.strip().split("\t")
    if len(parts) < 3:
        return None, None

    def _clean(value: str) -> str | None:
        # yt-dlp prints the literal "NA" for unset template fields.
        value = value.strip()
        return value or None if value != "NA" else None

    channel, channel_id, uploader = (_clean(p) for p in parts[:3])
    return (channel or uploader), channel_id


class YouTubeChannel(Channel):
    name = "youtube"
    description = "YouTube 视频和字幕"
    backends = ["yt-dlp"]
    tier = 0

    def can_handle(self, url: str) -> bool:
        from urllib.parse import urlparse

        d = urlparse(url).netloc.lower()
        return "youtube.com" in d or "youtu.be" in d

    def check(self, config=None):
        # 真跑 yt-dlp --version 探活，区分未装 / venv 断链 / 跑不动
        probe = probe_command("yt-dlp", ["--version"], timeout=10, package="yt-dlp")
        if probe.status == "missing":
            self.active_backend = None
            return "off", "yt-dlp 未安装。安装：pip install yt-dlp"
        if probe.status == "broken":
            self.active_backend = None
            return "error", f"yt-dlp 已安装但无法执行\n{probe.hint}"
        if not probe.ok:  # timeout / error：装了但跑不动
            self.active_backend = None
            detail = probe.hint or probe.output or probe.status
            return "error", f"yt-dlp 无法正常运行：{detail}"
        # yt-dlp 本体是活的；后面的 JS runtime/转写检查只影响 ok/warn，不影响后端归属
        self.active_backend = "yt-dlp"
        # Check JS runtime
        has_js = shutil.which("deno") or shutil.which("node")
        if not has_js:
            return "warn", (
                "yt-dlp 已安装但缺少 JS runtime（YouTube 必须）。\n"
                "  安装 Node.js 或 deno，然后运行：agent-reach install"
            )
        # Check yt-dlp config for --js-runtimes
        # Deno works out of the box; Node.js requires explicit config
        has_deno = shutil.which("deno")
        if not has_deno:
            ytdlp_config = get_ytdlp_config_path()
            if not _has_js_runtime_config(ytdlp_config):
                return "warn", (
                    f"yt-dlp 已安装但未配置 JS runtime。运行：\n  {render_ytdlp_fix_command()}"
                )
        # Surface transcription readiness so `doctor` reports it.
        msg = "可提取视频信息和字幕"
        if config is not None:
            providers = []
            if config.is_configured("groq_whisper"):
                providers.append("groq")
            if config.is_configured("openai_whisper"):
                providers.append("openai")
            if providers:
                if not shutil.which("ffmpeg"):
                    msg += "（音频转写需安装 ffmpeg）"
                else:
                    msg += f"，可转写音频（{'→'.join(providers)}）"
        return "ok", msg

    def transcribe(self, url: str, *, provider: str = "auto", config=None) -> str:
        """Download a YouTube video's audio and return its transcript.

        Delegates to :func:`agent_reach.transcribe.transcribe`. Imported lazily
        so the channel module stays cheap to import for users who never
        transcribe.
        """
        from agent_reach.transcribe import transcribe as _transcribe

        return _transcribe(url, provider=provider, config=config)

    def fetch_transcript(
        self,
        url: str,
        *,
        lang: str = "en",
        config=None,
        allow_whisper: bool = True,
    ) -> tuple[str | None, str | None]:
        """Return (transcript, source) for a video, trying cheapest source first.

        Order: manual subtitles → auto subtitles → Whisper transcription.
        source is one of "subtitles", "auto_subtitles", "whisper", or None when
        nothing is available. Whisper failures fall through to (None, None) so a
        single bad link can never raise out of a batch.
        """
        with tempfile.TemporaryDirectory(prefix="yt-subs-") as tmpdir:
            manual = _download_subtitles(url, lang, tmpdir, auto=False)
            if manual:
                return _clean_vtt(manual), "subtitles"

            auto = _download_subtitles(url, lang, tmpdir, auto=True)
            if auto:
                return _clean_vtt(auto), "auto_subtitles"

        if not allow_whisper:
            return None, None

        from agent_reach.transcribe import TranscribeError

        try:
            return self.transcribe(url, config=config), "whisper"
        except TranscribeError:
            return None, None
