# -*- coding: utf-8 -*-
"""Check that a quote an LLM gives is really in the transcript.

Matching ignores case, punctuation and spacing. A quote may join separate passages
with "..." or "…"; every passage must be found on its own.
"""

from __future__ import annotations

import re

_ELLIPSIS = re.compile(r"\.\.\.|…")
_PUNCTUATION = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _SPACES.sub(" ", _PUNCTUATION.sub("", text.lower())).strip()


def quote_found(quote: str, transcript: str) -> bool:
    fragments = [_normalize(f) for f in _ELLIPSIS.split(quote or "")]
    fragments = [f for f in fragments if f]
    if not fragments:
        return False
    haystack = _normalize(transcript or "")
    return all(fragment in haystack for fragment in fragments)
