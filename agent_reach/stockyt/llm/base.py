# -*- coding: utf-8 -*-
"""The LLM task, the backend interface, and the answer cache key."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LLMTask:
    id: str
    prompt: str
    schema: dict
    system: str = ""


class LLMError(RuntimeError):
    """The backend could not produce an answer (the call itself failed)."""


class InvalidAnswer(ValueError):
    """The backend returned something that is not a JSON answer; `ask` re-asks once."""


class LLMBackend(Protocol):
    def run(self, task: LLMTask) -> dict | None:
        """The answer, or None while it is pending (session backend)."""


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def cache_key(task: LLMTask, model: str | None) -> str:
    """Changes whenever the rendered prompt, the system prompt, the schema or the model change."""
    payload = canonical_json(
        {"prompt": task.prompt, "system": task.system, "schema": task.schema, "model": model or ""}
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
