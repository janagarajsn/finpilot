"""Rule 1: every LLM/embedding call goes through finpilot/llm/client.py → Mesh API."""

from __future__ import annotations

import ast
import re

from tests.conftest import REPO_ROOT, python_files

ALLOWED = {"finpilot/llm/client.py"}
SELF = {"tests/test_mesh_only.py"}

FORBIDDEN_MODULES = (
    "openai",
    "langfuse.openai",
    "anthropic",
    "google.generativeai",
    "google.genai",
    "vertexai",
    "cohere",
    "mistralai",
    "litellm",
    "groq",
    "together",
    "sentence_transformers",
    "fastembed",
    "langchain_openai",
    "langchain_anthropic",
    "langchain_google_genai",
    "llama_index.llms",
    "transformers",
    "ollama",
    "boto3",
)
LLM_URL = re.compile(
    r"https?://[^\s\"']*(api\.openai\.com|anthropic\.com|generativelanguage\.googleapis|"
    r"api\.mistral\.ai|api\.cohere|api\.groq\.com|openrouter\.ai|together\.xyz|meshapi\.ai)",
    re.I,
)


def _imports(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def _is_forbidden(module: str) -> bool:
    return any(module == m or module.startswith(m + ".") for m in FORBIDDEN_MODULES)


def test_no_model_sdk_imports_outside_client():
    violations = []
    for path in python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in ALLOWED | SELF:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        violations += [f"{rel}: import {m}" for m in _imports(tree) if _is_forbidden(m)]
    assert not violations, "model SDK imported outside llm/client.py:\n" + "\n".join(violations)


def test_no_llm_base_urls_outside_client():
    violations = []
    for path in python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in ALLOWED | SELF:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if LLM_URL.search(line):
                violations.append(f"{rel}:{i}: {line.strip()[:120]}")
    assert not violations, "LLM base URL outside llm/client.py:\n" + "\n".join(violations)


def test_client_targets_mesh_with_env_key():
    src = (REPO_ROOT / "finpilot/llm/client.py").read_text()
    assert 'MESH_BASE_URL = "https://api.meshapi.ai/v1"' in src
    assert "from langfuse.openai import OpenAI" in src
    assert "base_url=MESH_BASE_URL" in src
    assert "mesh_api_key" in src


def test_detector_catches_violations():
    bad = ast.parse(
        "import openai\nfrom langfuse.openai import OpenAI\nfrom anthropic import X\nimport fastembed"
    )
    assert all(_is_forbidden(m) for m in _imports(bad))
    assert not _is_forbidden("openpyxl")
    assert LLM_URL.search('base_url="https://api.openai.com/v1"')
