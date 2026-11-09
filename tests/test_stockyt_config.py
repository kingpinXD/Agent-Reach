# -*- coding: utf-8 -*-
"""Tests for stockyt settings: TOML over code defaults, and the run.json snapshot."""

import json
import os

import pytest

from agent_reach.stockyt import config


def test_no_file_gives_the_defaults(tmp_path):
    settings = config.load_settings(str(tmp_path / "missing.toml"))
    assert settings == config.Settings()
    assert settings.channels == config.CHANNELS
    assert settings.timezone == "America/New_York"
    assert settings.whisper is False
    assert settings.llm_backend == "claude-cli" and settings.llm_model is None
    assert settings.weights.coverage_smoothing_k == 1.0


def test_file_values_win_over_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'output_dir = "~/stockyt-out"\n'
        'pace_seconds = 0.5\n'
        'cookies_from_browser = "chrome"\n'
        'channels = [["Only", "UConly"]]\n'
        '[weights]\n'
        'thesis = 0.6\n',
        encoding="utf-8",
    )
    settings = config.load_settings(str(path))
    assert settings.output_dir == os.path.expanduser("~/stockyt-out")
    assert settings.cache_dir == os.path.join(settings.output_dir, ".cache", "channels")
    assert settings.pace_seconds == 0.5
    assert settings.cookies_from_browser == "chrome"
    assert settings.channels == [("Only", "UConly")]
    assert settings.weights.thesis == 0.6
    assert settings.weights.catalyst == 0.20  # untouched weights keep their default
    assert settings.lang == "en"


@pytest.mark.parametrize("text, message", [
    ("pace_secs = 1\n", "unknown setting(s) pace_secs"),
    ("[weights]\nthesys = 1\n", "unknown setting(s) thesys"),
])
def test_a_misspelled_key_is_an_error(tmp_path, text, message):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        config.load_settings(str(path))


def test_snapshot_is_plain_json():
    snap = config.Settings().snapshot()
    assert json.loads(json.dumps(snap)) == {**snap, "channels": [list(c) for c in config.CHANNELS]}
    assert snap["weights"]["coverage_smoothing_k"] == 1.0


def test_channel_lookups_still_work():
    assert config.name_for_id("UCoMzWLaPjDJBbipihD694pQ") == "Stock Moe"
    assert config.id_for_name("Stock Moe") == "UCoMzWLaPjDJBbipihD694pQ"
    assert config.name_for_id("UCnope") is None
