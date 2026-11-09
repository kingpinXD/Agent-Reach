# -*- coding: utf-8 -*-
"""Tests for the verbatim-quote check."""

from agent_reach.stockyt.tasks.quotes import quote_found

TRANSCRIPT = (
    "Oh boy. I expect it to return to the $ 16 to $20 range within the next 24-48 hours. "
    "This is what I'm counting on.  I have n't sold anything yet. Katie Stockton on the "
    "market sell-off.  Don't sell weakness, sell strength."
)


def test_exact_quote_is_found():
    assert quote_found("This is what I'm counting on.", TRANSCRIPT)


def test_case_punctuation_and_spacing_are_ignored():
    assert quote_found("i expect it to return to the $16 to $20 range", TRANSCRIPT)
    assert quote_found("THIS IS WHAT IM COUNTING ON", TRANSCRIPT)
    assert quote_found("market sell-off. Don't sell weakness", TRANSCRIPT)


def test_each_passage_joined_by_an_ellipsis_must_be_found():
    assert quote_found("Oh boy. ... I have n't sold anything yet.", TRANSCRIPT)
    assert quote_found("Oh boy… sell strength", TRANSCRIPT)
    assert not quote_found("Oh boy. ... I sold everything.", TRANSCRIPT)


def test_made_up_quote_is_not_found():
    assert not quote_found("I am loading up on Nvidia", TRANSCRIPT)


def test_words_must_be_in_order_and_adjacent():
    assert not quote_found("sell strength, don't sell weakness", TRANSCRIPT)


def test_empty_quote_is_not_found():
    assert not quote_found("", TRANSCRIPT)
    assert not quote_found(" ... ", TRANSCRIPT)
    assert not quote_found(None, TRANSCRIPT)
