"""Phase 0: one traced Mesh call inside an @observe span; prints where to find it in Langfuse."""

from __future__ import annotations

import sys

from langfuse import observe

from finpilot.config import get_settings
from finpilot.llm.client import get_llm
from finpilot.llm.schemas import HelloResponse
from finpilot.observability.langfuse import client, flush, init_langfuse, trace_context


@observe(name="hello-trace")
def run() -> tuple[str, str | None]:
    with trace_context(
        user_id="hello-user", session_id="hello-session", tags=["agent:probe", "hello"]
    ):
        parsed, _ = get_llm().chat_json(
            "system/hello",
            {"app_name": "FinPilot", "user_text": "hello trace"},
            HelloResponse,
            agent="probe",
        )
        return parsed.echo, client().get_current_trace_id()  # read while the span is active


def main() -> int:
    s = get_settings()
    init_langfuse(s)
    if not s.langfuse_configured:
        print(
            "Langfuse not configured (LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY); nothing will be exported."
        )
    echo, trace_id = run()
    flush()
    url = f"{s.langfuse_host}/project/{s.langfuse_init_project_id}/traces/{trace_id}"
    print(f"echo={echo!r}\ntrace_id={trace_id}\nopen: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
