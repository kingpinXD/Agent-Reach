# -*- coding: utf-8 -*-
"""Check that a quote an LLM gives is really in the transcript.

Matching ignores case, punctuation, symbols, invisible format characters (such as the
zero-width joiner) and spacing, after Unicode NFC normalisation. Combining marks stay: in
Devanagari they are the vowel signs, so dropping them would match different words. A quote may
join separate passages with "..." or "…"; every passage must be found on its own.
"""

from __future__ import annotations

import re
import unicodedata

_ELLIPSIS = re.compile(r"\.\.\.|…")
_SPACES = re.compile(r"\s+")


def _dropped(ch: str) -> bool:
    category = unicodedata.category(ch)
    return category[0] in "PS" or category == "Cf"


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text.lower())
    return _SPACES.sub(" ", "".join(ch for ch in text if not _dropped(ch))).strip()


def quote_found(quote: str, transcript: str) -> bool:
    fragments = [_normalize(f) for f in _ELLIPSIS.split(quote or "")]
    fragments = [f for f in fragments if f]
    if not fragments:
        return False
    haystack = _normalize(transcript or "")
    return all(fragment in haystack for fragment in fragments)
