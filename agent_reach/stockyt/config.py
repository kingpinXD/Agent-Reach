# -*- coding: utf-8 -*-
"""stockyt settings: the finance channels we track, where outputs live, and the tunables.

`load_settings()` reads `~/.config/stockyt/config.toml` over the code defaults below.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields

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
]

_NAME_BY_ID = {channel_id: name for name, channel_id in CHANNELS}
_ID_BY_NAME = {name: channel_id for name, channel_id in CHANNELS}


def name_for_id(channel_id: str) -> str | None:
    """Channel display name for an id, or None if untracked."""
    return _NAME_BY_ID.get(channel_id)


def id_for_name(name: str) -> str | None:
    """Channel id for a display name, or None if untracked."""
    return _ID_BY_NAME.get(name)


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
    whisper: bool = False
    pace_seconds: float = 5.0
    cookies_from_browser: str | None = None
    llm_backend: str = "claude-cli"
    llm_model: str | None = None
    llm_workers: int = 4
    chunk_chars: int = 60_000
    weights: Weights = field(default_factory=Weights)

    @property
    def cache_dir(self) -> str:
        return os.path.join(self.output_dir, ".cache", "channels")

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
    """Settings from the TOML file over the code defaults; defaults alone when there is no file.

    Python 3.10 has no tomllib, so there the file is not read and the defaults apply.
    """
    path = path or DEFAULT_CONFIG_PATH
    try:
        import tomllib
    except ImportError:
        return Settings()
    try:
        with open(path, "rb") as f:
            values = tomllib.load(f)
    except FileNotFoundError:
        return Settings()

    return _settings(values, path)


def _settings(values: dict, where: str) -> Settings:
    values = dict(_known(Settings, values, where))
    if "weights" in values:
        values["weights"] = Weights(**_known(Weights, values["weights"], f"{where} [weights]"))
    if "channels" in values:
        values["channels"] = [(name, channel_id) for name, channel_id in values["channels"]]
    if "output_dir" in values:
        values["output_dir"] = os.path.expanduser(values["output_dir"])
    return Settings(**values)
