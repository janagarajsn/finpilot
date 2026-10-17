"""Accessibility audit (axe-core, WCAG 2.1 A/AA) of every page in BOTH colour themes, against a
running stack. Not part of pytest.

Usage: make a11y              (needs `docker compose up`)
Creates a throwaway user with sample data, visits each page in light and dark mode (the admin-only
Pipeline and Quality pages as admin@finpilot.local from `make seed-demo`), and fails (exit 1) on
any violation. Details: e2e-shots/a11y-report.json (gitignored).
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from collections import Counter

from playwright.sync_api import sync_playwright

BASE = os.getenv("E2E_BASE_URL", "http://localhost:8000")
AXE = "https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js"
PUBLIC = ["/login", "/register", "/forgot"]
PAGES = [
    "/",
    "/app/insights",
    "/app/forecast",
    "/app/advice",
    "/app/chat",
    "/app/memory",
    "/app/transactions",
    "/app/categories",
    "/app/recurring",
    "/app/import",
    "/app/inbox",
    "/account",
]
ADMIN_PAGES = ["/app/pipeline", "/app/eval"]  # admin-only (seeded by `make seed-demo`)
ADMIN_EMAIL = os.getenv("E2E_ADMIN_EMAIL", "admin@finpilot.local")
ADMIN_PASSWORD = os.getenv("E2E_ADMIN_PASSWORD", os.getenv("DEMO_PASSWORD", "FinPilot-demo-2026!"))
RUN = "async () => (await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']}})).violations"


def audit(page, path: str, theme: str) -> list[dict]:
    page.goto(f"{BASE}{path}")
    page.evaluate("t => { localStorage.setItem('fp-theme', t); }", theme)
    page.reload()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)  # charts / Alpine settle
    page.add_script_tag(url=AXE)
    out = []
    for v in page.evaluate(RUN):
        out.append(
            {
                "page": path,
                "theme": theme,
                "rule": v["id"],
                "impact": v["impact"],
                "help": v["help"],
                "nodes": [
                    {
                        "target": n["target"],
                        "summary": (n.get("failureSummary") or "")[:300],
                        "html": n["html"][:200],
                    }
                    for n in v["nodes"][:8]
                ],
                "count": len(v["nodes"]),
            }
        )
    return out


def main() -> int:
    found: list[dict] = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(
            viewport={"width": 1280, "height": 900}, bypass_csp=True
        )  # to inject axe
        page = ctx.new_page()
        for path in PUBLIC:
            for theme in ("light", "dark"):
                found += audit(page, path, theme)
        page.goto(f"{BASE}/register")
        page.fill("#f-name", "A11y Tester")
        page.fill("#f-email", f"a11y-{int(time.time())}@example.com")
        page.fill("#f-password", "A11y-browser-passphrase-77")
        page.click("button[type=submit]")
        page.wait_for_url("**/onboarding")
        page.click("button[type=submit]")
        page.wait_for_url(f"{BASE}/")
        page.click("[data-load-sample]")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        for path in PAGES:
            for theme in ("light", "dark"):
                found += audit(page, path, theme)
        admin = b.new_context(viewport={"width": 1280, "height": 900}, bypass_csp=True).new_page()
        admin.goto(f"{BASE}/login")
        admin.fill("input[name=email]", ADMIN_EMAIL)
        admin.fill("input[name=password]", ADMIN_PASSWORD)
        admin.click("button[type=submit]")
        admin.wait_for_url(f"{BASE}/")
        for path in ADMIN_PAGES:
            for theme in ("light", "dark"):
                found += audit(admin, path, theme)
        b.close()
    pathlib.Path("e2e-shots").mkdir(exist_ok=True)
    with open("e2e-shots/a11y-report.json", "w") as f:
        json.dump(found, f, indent=1)
    by = Counter((v["rule"], v["theme"]) for v in found)
    for rule, theme in sorted(by):
        pages = sorted({v["page"] for v in found if v["rule"] == rule and v["theme"] == theme})
        nodes = sum(v["count"] for v in found if v["rule"] == rule and v["theme"] == theme)
        print(f"{rule:28} {theme:5} {nodes:4} nodes on {len(pages):2} pages: {', '.join(pages)}")
    print(f"VIOLATIONS {sum(v['count'] for v in found)}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
