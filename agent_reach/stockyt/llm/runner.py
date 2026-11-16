# -*- coding: utf-8 -*-
"""Ask a backend for a validated answer: cache, call, check, re-ask once, cache the valid answer.

`ask` returns (answer, errors):
  (answer, [])      a valid answer (fresh or cached);
  (None, [])        pending — the session backend is waiting for an agent;
  (answer|None, [...]) failed — invalid after the one re-ask, or the call itself failed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from agent_reach.stockyt.llm import cache
from agent_reach.stockyt.llm.base import InvalidAnswer, LLMBackend, LLMError, LLMTask, cache_key
from agent_reach.stockyt.llm.schema import schema_errors

Validator = Callable[[dict], list[str]]


def reask_task(task: LLMTask, errors: list[str]) -> LLMTask:
    """The same task under a new id, with the errors of the first answer appended to the prompt."""
    listed = "\n".join(f"- {e}" for e in errors)
    prompt = (
        f"{task.prompt}\n\nYour previous answer to this task was rejected for these reasons:\n"
        f"{listed}\n\nAnswer the whole task again, fixing every one of them."
    )
    return LLMTask(id=f"{task.id}-reask", prompt=prompt, schema=task.schema, system=task.system)


def _attempt(backend: LLMBackend, task: LLMTask, validate: Validator) -> tuple[dict | None, list[str]]:
    try:
        answer = backend.run(task)
    except InvalidAnswer as e:
        return {}, [str(e)]
    if answer is None:
        return None, []
    errors = schema_errors(answer, task.schema)
    return answer, errors or validate(answer)


def ask(backend: LLMBackend, task: LLMTask, validate: Validator, cache_dir: str,
        model: str | None) -> tuple[dict | None, list[str]]:
    key = cache_key(task, model)
    cached = cache.load(cache_dir, key)
    if cached is not None:
        return cached, []
    try:
        answer, errors = _attempt(backend, task, validate)
        if answer is not None and errors:
            answer, errors = _attempt(backend, reask_task(task, errors), validate)
    except LLMError as e:
        return None, [str(e)]
    if answer is not None and not errors:
        cache.save(cache_dir, key, answer)
    return answer, errors


def ask_many(backend: LLMBackend, jobs: list[tuple[LLMTask, Validator]], cache_dir: str,
             model: str | None, workers: int) -> dict[str, tuple[dict | None, list[str]]]:
    """`ask` for every (task, validator) pair in a pool of `workers` threads, keyed by task id."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {task.id: pool.submit(ask, backend, task, validate, cache_dir, model)
                   for task, validate in jobs}
        return {task_id: future.result() for task_id, future in futures.items()}
