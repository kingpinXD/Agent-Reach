# -*- coding: utf-8 -*-
"""Session backend: the program writes a prompt file, a session agent writes the answer file.

`run` writes `<dir>/<id>.prompt.md` and returns None until `<dir>/<id>.answer.json` exists.
"""

from __future__ import annotations

import json
import os

from agent_reach.stockyt.llm.base import InvalidAnswer, LLMTask
from agent_reach.stockyt.run import atomic_write_text


class SessionBackend:
    def __init__(self, directory: str):
        self.directory = os.path.abspath(directory)

    def prompt_path(self, task: LLMTask) -> str:
        return os.path.join(self.directory, f"{task.id}.prompt.md")

    def answer_path(self, task: LLMTask) -> str:
        return os.path.join(self.directory, f"{task.id}.answer.json")

    def render(self, task: LLMTask) -> str:
        parts = [
            task.system,
            task.prompt,
            f"Write ONLY a JSON value matching this schema to {self.answer_path(task)} — no prose, "
            "no code fence. Do not edit any other file.",
            "```json\n" + json.dumps(task.schema, ensure_ascii=False, indent=2) + "\n```",
        ]
        return "\n\n".join(part for part in parts if part) + "\n"

    def run(self, task: LLMTask) -> dict | None:
        atomic_write_text(self.prompt_path(task), self.render(task))
        answer_path = self.answer_path(task)
        if not os.path.exists(answer_path):
            return None
        with open(answer_path, encoding="utf-8") as f:
            text = f.read()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise InvalidAnswer(f"{os.path.basename(answer_path)} is not valid JSON: {e}") from e
