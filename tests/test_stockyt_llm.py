# -*- coding: utf-8 -*-
"""Tests for the stockyt LLM layer: cache key, cache, schema check, backends and the runner."""

import json
import subprocess

import pytest

from agent_reach.stockyt.llm import cache
from agent_reach.stockyt.llm.base import InvalidAnswer, LLMError, LLMTask, cache_key
from agent_reach.stockyt.llm.claude_cli import ClaudeCLIBackend, claude_argv
from agent_reach.stockyt.llm.fake import FakeBackend
from agent_reach.stockyt.llm.runner import ask, ask_many
from agent_reach.stockyt.llm.schema import schema_errors
from agent_reach.stockyt.llm.session import SessionBackend

SCHEMA = {
    "type": "object",
    "properties": {"n": {"type": "integer"}, "tag": {"type": ["string", "null"]}},
    "required": ["n"],
    "additionalProperties": False,
}
TASK = LLMTask(id="t1", prompt="Give n.", schema=SCHEMA, system="Answer in JSON.")


def _even(answer):
    return [] if answer["n"] % 2 == 0 else [f"n={answer['n']} is odd"]


# ── cache key ──

def test_cache_key_covers_prompt_system_schema_and_model():
    base = cache_key(TASK, "opus")
    assert cache_key(TASK, "opus") == base
    assert cache_key(LLMTask("t1", "Give n!", SCHEMA, TASK.system), "opus") != base
    assert cache_key(LLMTask("t1", TASK.prompt, SCHEMA, "other"), "opus") != base
    assert cache_key(LLMTask("t1", TASK.prompt, {**SCHEMA, "required": []}, TASK.system), "opus") != base
    assert cache_key(TASK, "sonnet") != base
    assert cache_key(TASK, None) != base


def test_cache_key_ignores_task_id_and_schema_key_order():
    reordered = dict(reversed(list(SCHEMA.items())))
    assert cache_key(LLMTask("other-id", TASK.prompt, reordered, TASK.system), "m") == cache_key(TASK, "m")


# ── cache ──

def test_cache_round_trip_and_miss(tmp_path):
    cache_dir = tmp_path / "llm"
    assert cache.load(str(cache_dir), "k") is None
    cache.save(str(cache_dir), "k", {"n": 2, "text": "é"})
    assert cache.load(str(cache_dir), "k") == {"n": 2, "text": "é"}
    assert [p.name for p in cache_dir.iterdir()] == ["k.json"]  # no temp file left behind


def test_cache_works_through_a_symlinked_folder(tmp_path):
    """macOS /tmp is a symlink to /private/tmp; the cache must not refuse it."""
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    cache.save(str(tmp_path / "link" / "llm"), "k", {"n": 2})
    assert cache.load(str(tmp_path / "real" / "llm"), "k") == {"n": 2}


# ── schema check ──

def test_schema_errors_accepts_a_valid_value():
    assert schema_errors({"n": 1, "tag": None}, SCHEMA) == []


@pytest.mark.parametrize("value, expected", [
    ([], "answer: expected object"),
    ({}, "answer: missing 'n'"),
    ({"n": "1"}, "answer.n: expected integer"),
    ({"n": True}, "answer.n: expected integer"),
    ({"n": 1, "tag": 3}, "answer.tag: expected string or null"),
    ({"n": 1, "extra": 1}, "answer: unexpected 'extra'"),
])
def test_schema_errors_reports_each_violation(value, expected):
    errors = schema_errors(value, SCHEMA)
    assert len(errors) == 1 and errors[0].startswith(expected)


def test_schema_errors_checks_enums_and_array_items():
    schema = {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}}
    assert schema_errors(["a", "b"], schema) == []
    assert schema_errors(["a", "c"], schema) == ["answer[1]: 'c' is not one of ['a', 'b']"]


# ── fake backend ──

def test_fake_backend_answers_by_id_and_records_calls():
    fake = FakeBackend({"t1": {"n": 2}, "t2": lambda task: {"n": len(task.prompt)}})
    assert fake.run(TASK) == {"n": 2}
    assert fake.run(LLMTask("t2", "abc", SCHEMA)) == {"n": 3}
    assert fake.run(LLMTask("t3", "x", SCHEMA)) is None
    assert [t.id for t in fake.calls] == ["t1", "t2", "t3"]


# ── session backend ──

def test_session_backend_writes_prompt_and_waits_for_the_answer(tmp_path):
    backend = SessionBackend(str(tmp_path))
    assert backend.run(TASK) is None
    prompt = (tmp_path / "t1.prompt.md").read_text(encoding="utf-8")
    assert prompt.startswith("Answer in JSON.\n\nGive n.\n\n")
    assert f"Write ONLY a JSON value matching this schema to {tmp_path / 't1.answer.json'}" in prompt
    assert '"required": [\n    "n"\n  ]' in prompt

    (tmp_path / "t1.answer.json").write_text('{"n": 4}', encoding="utf-8")
    assert backend.run(TASK) == {"n": 4}


def test_session_backend_rejects_an_answer_that_is_not_json(tmp_path):
    (tmp_path / "t1.answer.json").write_text("n is 4", encoding="utf-8")
    with pytest.raises(InvalidAnswer, match="t1.answer.json is not valid JSON"):
        SessionBackend(str(tmp_path)).run(TASK)


# ── runner ──

def test_ask_caches_a_valid_answer_and_reuses_it(tmp_path):
    fake = FakeBackend({"t1": {"n": 2}})
    assert ask(fake, TASK, _even, str(tmp_path), "m") == ({"n": 2}, [])
    assert ask(fake, TASK, _even, str(tmp_path), "m") == ({"n": 2}, [])
    assert len(fake.calls) == 1
    assert cache.load(str(tmp_path), cache_key(TASK, "m")) == {"n": 2}


def test_ask_reasks_once_with_the_errors_and_caches_under_the_original_key(tmp_path):
    fake = FakeBackend({"t1": {"n": 3}, "t1-reask": {"n": 4}})
    assert ask(fake, TASK, _even, str(tmp_path), "m") == ({"n": 4}, [])
    reask = fake.calls[1]
    assert reask.id == "t1-reask"
    assert reask.prompt.startswith("Give n.\n\nYour previous answer to this task was rejected")
    assert "- n=3 is odd" in reask.prompt
    assert reask.schema == SCHEMA and reask.system == TASK.system
    assert cache.load(str(tmp_path), cache_key(TASK, "m")) == {"n": 4}


def test_ask_gives_up_after_one_reask_and_caches_nothing(tmp_path):
    fake = FakeBackend({"t1": {"n": 3}, "t1-reask": {"n": 5}})
    assert ask(fake, TASK, _even, str(tmp_path / "llm"), "m") == ({"n": 5}, ["n=5 is odd"])
    assert len(fake.calls) == 2
    assert not (tmp_path / "llm").exists()


def test_ask_reasks_on_a_schema_violation_before_the_validator_runs(tmp_path):
    fake = FakeBackend({"t1": {"n": "two"}, "t1-reask": {"n": 2}})
    assert ask(fake, TASK, _even, str(tmp_path), "m") == ({"n": 2}, [])
    assert "answer.n: expected integer" in fake.calls[1].prompt


def test_ask_reasks_when_the_answer_is_not_json(tmp_path):
    (tmp_path / "t1.answer.json").write_text("{", encoding="utf-8")
    (tmp_path / "t1-reask.answer.json").write_text('{"n": 2}', encoding="utf-8")
    backend = SessionBackend(str(tmp_path))
    assert ask(backend, TASK, _even, str(tmp_path / "cache"), "m") == ({"n": 2}, [])
    assert "is not valid JSON" in (tmp_path / "t1-reask.prompt.md").read_text(encoding="utf-8")


def test_ask_pending_returns_none_without_errors_and_caches_nothing(tmp_path):
    cache_dir = str(tmp_path / "llm")
    assert ask(FakeBackend({}), TASK, _even, cache_dir, "m") == (None, [])
    pending_reask = FakeBackend({"t1": {"n": 3}})
    assert ask(pending_reask, TASK, _even, cache_dir, "m") == (None, [])
    assert not (tmp_path / "llm").exists()


def test_ask_turns_a_failed_call_into_an_error(tmp_path):
    def boom(task):
        raise LLMError("t1: claude -p exited 1: usage limit")

    assert ask(FakeBackend({"t1": boom}), TASK, _even, str(tmp_path), "m") == (
        None, ["t1: claude -p exited 1: usage limit"])


def test_ask_many_answers_every_task_keyed_by_id(tmp_path):
    tasks = [LLMTask(f"t{i}", f"p{i}", SCHEMA) for i in range(6)]
    fake = FakeBackend({t.id: (lambda task: {"n": int(task.prompt[1:]) * 2}) for t in tasks})
    results = ask_many(fake, [(t, _even) for t in tasks], str(tmp_path), "m", workers=3)
    assert results == {f"t{i}": ({"n": i * 2}, []) for i in range(6)}


# ── claude CLI backend ──

class _FakeRun:
    def __init__(self, returncode=0, stdout="", stderr="", raises=None):
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        self.raises = raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises:
            raise self.raises
        return self.result


def _ok(output):
    return json.dumps({"type": "result", "is_error": False, "result": json.dumps(output),
                       "structured_output": output})


def test_claude_argv_uses_the_settled_flags_and_never_bare():
    argv = claude_argv(TASK, None)
    assert argv == [
        "claude", "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
        "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
        "--tools", "", "--system-prompt", "Answer in JSON.",
    ]
    assert claude_argv(TASK, "sonnet")[-2:] == ["--model", "sonnet"]
    assert "--bare" not in claude_argv(TASK, "sonnet")


def test_claude_backend_sends_the_prompt_on_stdin_and_returns_structured_output():
    run = _FakeRun(stdout=_ok({"n": 2}))
    backend = ClaudeCLIBackend(model="opus", timeout=30, run=run)
    assert backend.run(TASK) == {"n": 2}
    argv, kwargs = run.calls[0]
    assert argv == claude_argv(TASK, "opus")
    assert kwargs["input"] == "Give n." and kwargs["timeout"] == 30
    assert TASK.prompt not in argv


@pytest.mark.parametrize("run, message", [
    (_FakeRun(returncode=1, stderr="boom\n" + "x" * 3000 + "TAIL"), "t1: claude -p exited 1: x"),
    (_FakeRun(stdout=json.dumps({"is_error": True, "result": "Credit balance too low"})),
     "t1: claude -p reported an error: Credit balance too low"),
    (_FakeRun(stdout="not json"), "t1: claude -p printed no JSON: not json"),
    (_FakeRun(stdout=json.dumps({"is_error": False, "result": "hi"})),
     "t1: claude -p returned no structured_output: hi"),
    (_FakeRun(raises=subprocess.TimeoutExpired("claude", 5)), "t1: claude -p timed out after 600s"),
    (_FakeRun(raises=FileNotFoundError("claude")), "t1: could not start claude"),
])
def test_claude_backend_raises_a_clear_error(run, message):
    with pytest.raises(LLMError) as err:
        ClaudeCLIBackend(run=run).run(TASK)
    assert str(err.value).startswith(message)


def test_claude_backend_error_keeps_only_the_stderr_tail():
    run = _FakeRun(returncode=2, stderr="HEAD" + "x" * 5000 + "TAIL")
    with pytest.raises(LLMError) as err:
        ClaudeCLIBackend(run=run).run(TASK)
    assert str(err.value).endswith("TAIL") and "HEAD" not in str(err.value)
