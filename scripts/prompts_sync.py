"""Push every prompt file to Langfuse Prompt Management. Files remain the source of truth;
each Langfuse prompt is labelled with the file version and content hash."""

from __future__ import annotations

import sys

from finpilot.llm.prompt_registry import get_registry
from finpilot.observability.langfuse import client, flush, langfuse_enabled


def main() -> int:
    if not langfuse_enabled():
        print("Langfuse not configured; skipping (local files are used at runtime).")
        return 0
    lf = client()
    for p in sorted(get_registry().all(), key=lambda p: (p.key, p.meta.version)):
        lf.create_prompt(
            name=p.key,
            type="chat",
            prompt=[
                {"role": "system", "content": p.static_src.strip()},
                {"role": "user", "content": p.dynamic_src.strip()},
            ],
            labels=[f"v{p.meta.version}", "production"],
            tags=[p.agent],
            config={**p.meta.model_dump(), "content_hash": p.content_hash},
        )
        print(f"synced {p.ref}")
    flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
