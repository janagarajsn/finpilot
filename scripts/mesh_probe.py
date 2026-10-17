"""Phase 0 check: verify Mesh chat (JSON mode), cached_tokens passthrough, and /embeddings.

Usage: `make mesh-probe` (needs MESH_API_KEY). Prints a JSON report to paste into DESIGN_NOTES.
"""

from __future__ import annotations

import json
import sys

from finpilot.llm.client import get_llm
from finpilot.llm.schemas import HelloResponse
from finpilot.observability.langfuse import flush


def main() -> int:
    llm = get_llm()
    report: dict[str, object] = {"mesh_configured": llm.available}
    if not llm.available:
        print(json.dumps({**report, "error": "MESH_API_KEY not set"}, indent=2))
        return 2
    try:
        parsed, res = llm.chat_json(
            "system/hello",
            {"app_name": "FinPilot", "user_text": "ping"},
            HelloResponse,
            agent="probe",
        )
        report["chat"] = {"ok": parsed.ok, "model": res.model, "usage": res.usage}
        # second identical call: does the provider report cached prompt tokens?
        _, res2 = llm.chat_json(
            "system/hello",
            {"app_name": "FinPilot", "user_text": "ping"},
            HelloResponse,
            agent="probe",
        )
        report["chat_repeat_usage"] = res2.usage
    except Exception as e:
        report["chat"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    ok = llm.probe_embeddings()
    report["embeddings_available"] = ok
    if ok:
        vec = llm.embed(["hello"], agent="probe")[0]
        report["embedding_dim"] = len(vec)
    report["recommendation"] = (
        "EMBEDDINGS_ENABLED=true" if ok else "EMBEDDINGS_ENABLED=false (lexical fallback)"
    )
    flush()
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
