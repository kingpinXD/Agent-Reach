# -*- coding: utf-8 -*-
"""ClaudeCLI backend: one `claude -p` subprocess per task, on the user's existing Claude login.

The flags keep the user's CLAUDE.md, hooks, MCP servers and tools out of the call and stop it
saving a session. `--bare` is not used: it drops the OAuth login. The prompt goes on stdin
because transcripts are too large for argv.
"""

from __future__ import annotations

import json
import subprocess
from typing import Callable

from agent_reach.stockyt.llm.base import LLMError, LLMTask
from agent_reach.stockyt.upstream import utf8_subprocess_env

_TAIL = 2000


def claude_argv(task: LLMTask, model: str | None, binary: str = "claude") -> list[str]:
    argv = [
        binary, "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(task.schema, ensure_ascii=False),
        "--setting-sources", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--tools", "",
        "--system-prompt", task.system,
    ]
    if model:
        argv += ["--model", model]
    return argv


def _tail(text: str | None) -> str:
    return (text or "").strip()[-_TAIL:]


class ClaudeCLIBackend:
    def __init__(self, model: str | None = None, timeout: float = 600, binary: str = "claude",
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.model = model
        self.timeout = timeout
        self.binary = binary
        self._run = run

    def run(self, task: LLMTask) -> dict:
        argv = claude_argv(task, self.model, self.binary)
        try:
            proc = self._run(argv, input=task.prompt, capture_output=True, encoding="utf-8",
                             errors="replace", timeout=self.timeout, env=utf8_subprocess_env())
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"{task.id}: claude -p timed out after {self.timeout:g}s") from e
        except OSError as e:
            raise LLMError(f"{task.id}: could not start {self.binary}: {e}") from e

        if proc.returncode != 0:
            raise LLMError(f"{task.id}: claude -p exited {proc.returncode}: "
                           f"{_tail(proc.stderr) or _tail(proc.stdout)}")
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(f"{task.id}: claude -p printed no JSON: {_tail(proc.stdout)}") from e
        if result.get("is_error"):
            raise LLMError(f"{task.id}: claude -p reported an error: "
                           f"{_tail(str(result.get('result')))} {_tail(proc.stderr)}".strip())
        if "structured_output" not in result:
            raise LLMError(f"{task.id}: claude -p returned no structured_output: "
                           f"{_tail(str(result.get('result')))}")
        return result["structured_output"]
