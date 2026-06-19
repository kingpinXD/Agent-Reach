# -*- coding: utf-8 -*-
"""The finance channels we track, in display order.

Plain data module — no behaviour beyond name<->id lookup.
"""

from __future__ import annotations

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
