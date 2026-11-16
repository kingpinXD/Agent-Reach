# -*- coding: utf-8 -*-
"""stockyt settings: the finance channels we track, where outputs live, and the tunables.

`load_settings()` reads `~/.config/stockyt/config.toml` over the code defaults below.
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass, field, fields, replace

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# Where pipeline runs, outputs and the channel cache live.
OUTPUT_DIR = os.path.expanduser("~/Documents/Claude/Cowork Projects/YoutubeSummaries")

# (display name, YouTube channel id). Order is preserved everywhere downstream.
CHANNELS: list[tuple[str, str]] = [
    ("Tom Nash", "UCJwKCyEIFHwUOPQQ-4kC1Zw"),
    ("ClearValue Tax", "UCigUBIf-zt_DA6xyOQtq2WA"),
    ("Meet Kevin", "UCUvvj5lwue7PspotMDjk5UA"),
    ("Financial Education", "UCnMn36GT_H0X-w5_ckLtlgQ"),
    ("Joseph Carlson", "UCbta0n8i6Rljh0obO7HzG9A"),
    ("Ticker Symbol: YOU", "UC7kCeZ53sli_9XwuQeFxLqw"),
    ("Stock Moe", "UCoMzWLaPjDJBbipihD694pQ"),
    ("Ricky Gutierrez", "UCtlAFoYl2aWb6pMiHCctQHA"),
    ("Adam Khoo", "UCK-aOjEvZNJl3HINja0gAiQ"),
    ("New Money", "UCvSXMi2LebwJEM1s4bz5IBA"),
    ("ZipTrader", "UC0BGhWsIbV7Dm-lsvhdlMbA"),
]

# The Indian market's channels. NDTV Profit and Zee Business are left out: each posts 10+ videos a
# day from many guest analysts under one name.
INDIA_CHANNELS: list[tuple[str, str]] = [
    ("CNBC-TV18", "UCmRbHAgG2k2vDUvb3xsEunQ"),
    ("ET Now", "UCI_mwTKUhicNzFrhm33MzBQ"),
    ("Trade Brains", "UCzw35O6toJtjqEAAt4LTjKQ"),
    ("SOIC", "UCB7GnQlJPIL6rBBqEoX87vA"),
    ("Parimal Ade", "UCUWbS9qoyLKTdOlvgK1GBlA"),
    ("Yadnya Investment Academy", "UCPohbSYq4IXhv0yxiy-sT4g"),
    ("FinnovationZ by Prasad", "UCUMccND2H_CVS0dMZKCPCXA"),
    ("Dr. Mukul Agrawal", "UCPs8w9f1gqe4BqkbI-9wTnw"),
    ("Rakesh Bansal", "UC6HlGuJZ0_M_rSgm0hoRhDQ"),
    ("Stock Pro", "UCQIRijkQ8zNDe_jR0RhZpVA"),
]

MARKET_NAMES = {"us": "US", "india": "India"}

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/stockyt/config.toml")


@dataclass(frozen=True)
class Weights:
    """Grading weights: per-mention context score parts, and the coverage smoothing K (m / (n + K))."""

    thesis: float = 0.45
    catalyst: float = 0.20
    price_target: float = 0.15
    primary: float = 0.10
    horizon: float = 0.10
    coverage_smoothing_k: float = 1.0


@dataclass(frozen=True)
class Settings:
    output_dir: str = OUTPUT_DIR
    timezone: str = "America/New_York"
    channels: list[tuple[str, str]] = field(default_factory=lambda: list(CHANNELS))
    lang: str = "en"
    caption_langs: list[str] | None = None  # None: youtube.caption_langs(lang)
    whisper: bool = False
    pace_seconds: float = 5.0
    cookies_from_browser: str | None = None
    llm_backend: str = "claude-cli"
    llm_model: str | None = None
    llm_workers: int = 4
    chunk_chars: int = 60_000
    weights: Weights = field(default_factory=Weights)
    market: str = "us"

    @property
    def cache_dir(self) -> str:
        return os.path.join(self.output_dir, ".cache", "channels")

    def channel_name(self, channel_id: str | None) -> str | None:
        """Display name of a tracked channel, or None if untracked."""
        return next((name for name, cid in self.channels if cid == channel_id), None)

    def snapshot(self) -> dict:
        """Plain-JSON copy for run.json."""
        return asdict(self)

    @classmethod
    def from_snapshot(cls, values: dict) -> "Settings":
        """Settings back from a run.json snapshot."""
        return _settings(dict(values), "run.json config_snapshot")


def _known(cls, values: dict, where: str) -> dict:
    unknown = set(values) - {f.name for f in fields(cls)}
    if unknown:
        raise ValueError(f"unknown setting(s) {', '.join(sorted(unknown))} in {where}")
    return values


def load_settings(path: str | None = None) -> Settings:
    """Settings from the TOML file over the code defaults; defaults alone when there is no file."""
    path = path or DEFAULT_CONFIG_PATH
    try:
        with open(path, "rb") as f:
            values = tomllib.load(f)
    except FileNotFoundError:
        return Settings()

    if "market" in values:
        raise ValueError(f"market in {path}: the market is chosen with --market, not in the config file")
    return _settings(values, path)


def apply_market(settings: Settings, market: str) -> Settings:
    """`settings` for a market: unchanged for us; India's channels, timezone and caption tracks for india.

    India never asks for the auto-translated `en` track: YouTube rate limits it (GOTCHAS).
    """
    if market not in MARKET_NAMES:
        raise ValueError(f"unknown market {market!r}; use one of {', '.join(MARKET_NAMES)}")
    if market == "us":
        return settings
    return replace(settings, market=market, channels=list(INDIA_CHANNELS), timezone="Asia/Kolkata",
                   caption_langs=["en-orig", "hi-orig"])


def _settings(values: dict, where: str) -> Settings:
    values = dict(_known(Settings, values, where))
    if "weights" in values:
        values["weights"] = Weights(**_known(Weights, values["weights"], f"{where} [weights]"))
    if "channels" in values:
        values["channels"] = [(name, channel_id) for name, channel_id in values["channels"]]
    if "output_dir" in values:
        values["output_dir"] = os.path.expanduser(values["output_dir"])
    return Settings(**values)
