"""Rule 3: no prompt text inside .py files (prompts/, config/, templates/ only)."""

from __future__ import annotations

import ast
import re

from tests.conftest import REPO_ROOT, python_files

SELF = {"tests/test_no_inline_prompts.py"}
PROMPTISH = re.compile(
    r"(^\s*you are\b|\{\{|\{%|<data>|\brespond (only )?with\b|\breply with\b|\bfew-shot\b|"
    r"\bas an ai\b|\bsystem prompt\b|\bignore (all )?previous\b)",
    re.I | re.M,
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def find_inline_prompts(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    lines = source.splitlines()
    hits = []
    for node in ast.walk(tree):
        if (
            not (isinstance(node, ast.Constant) and isinstance(node.value, str))
            or id(node) in docstrings
        ):
            continue
        line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
        triple = '"""' in line or "'''" in line
        long = len(node.value) > 200
        if ((triple or long) and PROMPTISH.search(node.value)) or (
            PROMPTISH.search(node.value) and re.match(r"^\s*you are\b", node.value, re.I)
        ):
            hits.append((node.lineno, node.value[:60]))
    return hits


def test_no_inline_prompt_text():
    violations = []
    for path in python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in SELF or rel.startswith("tests/"):
            continue
        violations += [
            f"{rel}:{ln}: {snip!r}" for ln, snip in find_inline_prompts(path.read_text("utf-8"))
        ]
    assert not violations, "inline prompt text found:\n" + "\n".join(violations)


def test_detector_flags_prompts_and_allows_docstrings():
    src = 'def f():\n    """You are a docstring and that is fine."""\n    x = """You are a helpful bot.\nUse {{ var }}"""\n'
    hits = find_inline_prompts(src)
    assert len(hits) == 1 and hits[0][0] == 3
    assert find_inline_prompts('X = "You are the categorizer"')
    assert not find_inline_prompts('SQL = """SELECT * FROM t WHERE a = :a"""')
