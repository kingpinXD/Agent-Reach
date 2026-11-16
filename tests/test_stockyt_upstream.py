# -*- coding: utf-8 -*-
"""Only stockyt/upstream.py may import upstream Agent-Reach's channels and runtime."""

import ast
from pathlib import Path

import agent_reach.stockyt as stockyt

STOCKYT_DIR = Path(stockyt.__file__).parent
GUARDED = ("agent_reach.channels", "agent_reach.runtime", "agent_reach.utils")


def _upstream_imports(path: Path) -> list[str]:
    """Guarded upstream modules a file imports."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        else:
            continue
        found += [n for n in names if n in GUARDED or n.startswith(tuple(g + "." for g in GUARDED))]
    return found


def test_only_the_adapter_imports_upstream():
    offenders = {
        str(path.relative_to(STOCKYT_DIR)): imports
        for path in sorted(STOCKYT_DIR.rglob("*.py"))
        if path.name != "upstream.py" and (imports := _upstream_imports(path))
    }
    assert offenders == {}, f"import these through agent_reach/stockyt/upstream.py instead: {offenders}"


def test_the_scan_sees_upstream_imports(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from agent_reach.channels.youtube import x\nimport agent_reach.runtime.result\n"
        "import agent_reach.runtime\nfrom agent_reach.stockyt import config\nfrom agent_reach.channelsx import y\n"
        "from agent_reach import runtime\nfrom . import grading\n",
        encoding="utf-8",
    )
    assert _upstream_imports(sample) == [
        "agent_reach.channels.youtube", "agent_reach.channels.youtube.x", "agent_reach.runtime.result",
        "agent_reach.runtime", "agent_reach.runtime"]
