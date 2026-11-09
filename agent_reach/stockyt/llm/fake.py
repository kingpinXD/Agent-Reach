# -*- coding: utf-8 -*-
"""Canned answers for tests."""

from __future__ import annotations

from typing import Callable

from agent_reach.stockyt.llm.base import LLMTask

Answer = dict | Callable[[LLMTask], dict | None]


class FakeBackend:
    """Answers by task id: a dict, or a callable given the task. An unknown id is pending (None).

    Every task it receives is kept in `calls`, so a test can see the re-ask prompt.
    """

    def __init__(self, answers: dict[str, Answer]):
        self.answers = answers
        self.calls: list[LLMTask] = []

    def run(self, task: LLMTask) -> dict | None:
        self.calls.append(task)
        answer = self.answers.get(task.id)
        return answer(task) if callable(answer) else answer
