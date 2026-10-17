"""Browser smoke test (Playwright) against a running stack. Not part of pytest.

Usage: make e2e            (needs `docker compose up`; installs Chromium on first run)
Creates a throwaway user e2e-<ts>@example.com; fails on any console error / JS exception.
Screenshots go to the directory given as argv[1] (default: ./e2e-shots, gitignored).
"""

import os
import pathlib
import re
import sys
import time

from playwright.sync_api import expect, sync_playwright

BASE = os.getenv("E2E_BASE_URL", "http://localhost:8000")
ADMIN_EMAIL = os.getenv("E2E_ADMIN_EMAIL", "admin@finpilot.local")  # `make seed-demo`
ADMIN_PASSWORD = os.getenv("E2E_ADMIN_PASSWORD", os.getenv("DEMO_PASSWORD", "FinPilot-demo-2026!"))
OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "e2e-shots")
OUT.mkdir(parents=True, exist_ok=True)
email = f"e2e-{int(time.time())}@example.com"
errors = []

with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1280, "height": 900})
    page.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("dialog", lambda d: d.accept())

    page.goto(f"{BASE}/register")
    page.fill("#f-name", "E2E Tester")
    page.fill("#f-email", email)
    page.fill("#f-password", "E2e-browser-passphrase-77")
    page.click("button[type=submit]")
    page.wait_for_url("**/onboarding")
    page.click("button[type=submit]")
    page.wait_for_url(f"{BASE}/")
    page.screenshot(path=OUT / "01-dashboard-empty.png", full_page=True)

    # Load sample data → dashboard fills, most rows categorized instantly (rules + memory)
    page.click("[data-load-sample]")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#trend-chart")).to_be_visible(timeout=20000)
    page.wait_for_function("() => window.Chart && document.querySelector('#category-chart')")
    page.screenshot(path=OUT / "01b-dashboard-sample.png", full_page=True)

    # Insights (analyzer): anomalies, subscriptions, patterns; evidence link opens the transactions
    expect(page.locator("#sample")).to_have_count(0)  # the sample-data card is gone once loaded
    page.goto(f"{BASE}/app/insights")
    expect(page.locator("h1", has_text="Insights")).to_be_visible()
    page.wait_for_function(
        "() => window.Chart && document.querySelector('#weekly-chart').width > 0"
    )
    page.wait_for_timeout(1200)
    page.screenshot(path=OUT / "11-insights.png", full_page=True)
    ev = page.locator("a", has_text="See ").first
    if ev.count():
        ev.click()
        page.wait_for_url("**/app/transactions?ids=*")

    # Advice: generate (smart LLM via Mesh behind the grounding guard), open the trail, dismiss one
    page.goto(f"{BASE}/app/advice")
    # "Get advice", or "Refresh advice" if the worker already wrote some; wait for the new run
    with page.expect_response(lambda r: r.url.endswith("/recommendations/refresh"), timeout=120000):
        page.click("button:has-text('advice')")
    page.wait_for_load_state("networkidle")  # the page reloads with the new advice
    expect(page.locator("ol li").first).to_be_visible(timeout=30000)
    first_rec = page.locator("ol > li").first
    first_rec.locator("button:has-text('Why?')").click()
    expect(first_rec.locator("text=Findings it rests on")).to_be_visible(timeout=10000)
    expect(first_rec.locator("text=checked against computed facts")).to_be_visible()
    page.screenshot(path=OUT / "14-advice.png", full_page=True)
    last_rec = page.locator("ol > li").last
    last_rec.locator("button:has-text('Not for me')").click()
    last_rec.locator("input[placeholder^='Why not?']").fill("Shared with family")
    with page.expect_navigation():  # dismiss → POST → the page reloads itself
        last_rec.locator("button:has-text('Dismiss')").click()
    page.wait_for_load_state("networkidle")
    page.goto(f"{BASE}/app/insights")
    expect(page.locator("#sum-h + p")).not_to_have_text("Writing a short summary…", timeout=60000)

    # Pipeline and Quality are admin-only: hidden from this user's sidebar
    expect(page.locator("aside a[href='/app/pipeline']")).to_have_count(0)
    expect(page.locator("aside a[href='/app/eval']")).to_have_count(0)
    # Admin: pipeline trace, run every agent now and follow it live (SSE), then the timeline
    admin = b.new_page(viewport={"width": 1280, "height": 900})
    admin.on("console", lambda m: m.type == "error" and errors.append(f"admin console: {m.text}"))
    admin.on("pageerror", lambda e: errors.append(f"admin pageerror: {e}"))
    admin.goto(f"{BASE}/login")
    admin.fill("input[name=email]", ADMIN_EMAIL)
    admin.fill("input[name=password]", ADMIN_PASSWORD)
    admin.click("button[type=submit]")
    admin.wait_for_url(f"{BASE}/")
    expect(admin.locator("aside a[href='/app/pipeline']").last).to_be_visible()
    admin.goto(f"{BASE}/app/pipeline")
    expect(admin.locator("h1", has_text="Pipeline trace")).to_be_visible()
    admin.click("button:has-text('Run now')")
    expect(admin.locator("text=Live run")).to_be_visible(timeout=10000)
    admin.wait_for_url("**/app/pipeline?run=*", timeout=180000)  # reloads when the run ends
    expect(admin.locator("ol li", has_text="persist trail").first).to_be_visible()
    admin.screenshot(path=OUT / "15-pipeline.png", full_page=True)

    # Recurring: detected repeats → promote one to a declared rule
    page.goto(f"{BASE}/app/recurring")
    expect(page.locator("h2#sugg-h")).to_be_visible()
    page.screenshot(path=OUT / "12-recurring-detected.png", full_page=True)
    first = page.locator("#sugg-h ~ ul li").first
    name = first.locator(".font-medium").inner_text()
    first.locator("text=Make it recurring").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("article h2", has_text=name.title()).first).to_be_visible()

    # Forecast & what-if: band chart, structured simulation, a goal
    page.goto(f"{BASE}/app/forecast")
    expect(page.locator("h1", has_text="Forecast")).to_be_visible()
    page.wait_for_function(
        "() => window.Chart && document.querySelector('#forecast-chart').width > 0"
    )
    page.locator("summary", has_text="Use the form").click()
    page.locator("form select[x-model='form.type']").select_option("one_time_purchase")
    page.locator("input[x-model='form.amount']").fill("150000")
    page.locator("button", has_text="Simulate").click()
    # the answer is LLM-written under the grounding guard (template if rejected): it states amounts
    expect(page.locator("[aria-live=polite] p")).to_contain_text(
        re.compile(r"[₹$]\s?-?[\d,]+"), timeout=30000
    )
    page.click("text=+ New goal")
    page.fill("input[x-model='g.name']", "Emergency fund")
    page.fill("input[x-model='g.target_amount']", "300000")
    page.click("button:has-text('Save goal')")
    page.wait_for_load_state("networkidle")
    expect(page.locator("li", has_text="Emergency fund").first).to_be_visible()
    page.wait_for_timeout(1200)
    page.screenshot(path=OUT / "13-forecast.png", full_page=True)
    page.goto(f"{BASE}/")

    # Period dropdown
    page.locator("main summary", has_text="This month").click()
    page.locator("nav[aria-label=Period] a", has_text="Last 3 months").click()
    page.wait_for_url("**/?period=3m")
    expect(page.locator("main summary", has_text="Last 3 months")).to_be_visible()

    # Chat: a free-form question goes through the tool loop (or a clear notice without Mesh);
    # a follow-up in the same session; then the Memory page
    page.goto(f"{BASE}/app/chat")
    page.locator("button", has_text="How much did I spend on dining in the last 3 months?").click()
    reply = page.locator("[x-data=chatPage] .justify-start p.whitespace-pre-line").first
    expect(reply).not_to_be_empty(timeout=120000)
    page.wait_for_url("**/app/chat?session=*")
    page.fill("#chat-q", "And what about groceries?")
    page.locator("button", has_text="Send").click()
    expect(page.locator("[x-data=chatPage] .justify-start p.whitespace-pre-line")).to_have_count(
        2, timeout=120000
    )
    page.screenshot(path=OUT / "17-chat.png", full_page=True)
    page.goto(f"{BASE}/app/memory")
    expect(page.locator("h1", has_text="What FinPilot remembers")).to_be_visible()
    page.screenshot(path=OUT / "18-memory.png", full_page=True)

    # Inbox (digest history + alerts) via the header bell; Telegram linking in Settings
    page.goto(f"{BASE}/")
    page.locator("header a[title='Digests and alerts']").click()
    expect(page.locator("h1", has_text="Inbox")).to_be_visible()
    page.screenshot(path=OUT / "19-inbox.png", full_page=True)
    page.goto(f"{BASE}/account")
    page.locator("button", has_text="Link Telegram").click()
    expect(page.locator("[x-data=telegramLink]")).to_contain_text("/start", timeout=10000)
    page.goto(f"{BASE}/")

    # Add USD expense via modal: live FX preview
    page.click("main button[data-add-txn=expense]")
    modal = page.locator("[x-data=txnModal] form")
    expect(modal).to_be_visible()
    modal.locator("input[type=number]").first.fill("12")
    modal.locator("select").first.select_option("USD")
    modal.locator("input[placeholder^='e.g. Swiggy']").fill("OPENAI *CHATGPT")
    expect(modal.locator("p[x-show=fxPreview]")).to_contain_text("≈ ₹", timeout=8000)
    page.screenshot(path=OUT / "02-modal-fx-preview.png")
    modal.locator("button[type=submit]").click()
    page.wait_for_load_state("networkidle")

    # Add INR expense with repeat monthly
    page.click("main button[data-add-txn=expense]")
    modal.locator("input[type=number]").first.fill("649")
    modal.locator("input[placeholder^='e.g. Swiggy']").fill("NETFLIX.COM")
    modal.locator("text=Repeat this").click()
    modal.locator("button[type=submit]").click()
    page.wait_for_load_state("networkidle")
    page.click("main button[data-add-txn=income]")
    modal.locator("input[type=number]").first.fill("85000")
    modal.locator("input[placeholder^='e.g. Swiggy']").fill("SALARY ACME LTD")
    modal.locator("button[type=submit]").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#trend-chart")).to_be_visible()
    page.wait_for_function("() => document.querySelector('#trend-chart').width > 0 && window.Chart")
    page.screenshot(path=OUT / "03-dashboard-data.png", full_page=True)

    # Transactions: inline category change (confirm dialog auto-accepted → apply to merchant)
    page.goto(f"{BASE}/app/transactions?q=OPENAI")
    row = page.locator("tr", has_text="OPENAI").first
    row.locator("select").select_option(label="Subscriptions")
    expect(row.locator("[x-text=msg]")).to_contain_text("Saved", timeout=5000)
    page.screenshot(path=OUT / "04-transactions.png", full_page=True)

    # Categories: create custom category with keywords
    page.goto(f"{BASE}/app/categories")
    page.click("text=+ New category")
    page.fill("form >> input[x-model='form.name']", "AI Tools")
    page.fill("form >> input[x-model='form.keywords']", "OPENAI, ANTHROPIC")
    page.click("form >> button:has-text('Save')")
    page.wait_for_load_state("networkidle")
    page.screenshot(path=OUT / "05-categories.png", full_page=True)

    # Recurring: create a rule
    page.goto(f"{BASE}/app/recurring")
    expect(page.locator("text=NETFLIX.COM").first).to_be_visible()
    page.click("text=+ New recurring item")
    page.fill("input[x-model='form.description']", "Rent via NEFT")
    page.fill("input[x-model='form.amount']", "25000")
    page.click("form >> button:has-text('Save')")
    page.wait_for_load_state("networkidle")
    expect(page.locator("h2", has_text="Rent via NEFT")).to_be_visible()
    page.screenshot(path=OUT / "06-recurring.png", full_page=True)

    # Import wizard: template → upload → preview → import → undo
    page.goto(f"{BASE}/app/import")
    tpl = page.request.get(f"{BASE}/import/template.csv").body()
    (OUT / "tpl.csv").write_bytes(tpl)
    page.set_input_files("input[type=file]", str(OUT / "tpl.csv"))
    expect(page.locator("h2#pv-h")).to_be_visible(timeout=10000)
    page.screenshot(path=OUT / "07-import-preview.png", full_page=True)
    page.click("button:has-text('Import ')")
    expect(page.locator("text=Import complete")).to_be_visible(timeout=20000)
    page.screenshot(path=OUT / "08-import-done.png", full_page=True)
    page.goto(f"{BASE}/app/import")
    page.locator("text=Undo this import").first.click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("text=undone").first).to_be_visible()

    # System quality page (eval results, admin) renders with charts
    user_page, page = page, admin
    page.goto(f"{BASE}/app/eval")
    expect(page.locator("h1", has_text="System quality")).to_be_visible()
    page.wait_for_timeout(1500)  # let Chart.js finish its grow-in animation before the screenshot
    page.screenshot(path=OUT / "10-eval.png", full_page=True)
    if page.locator("#calibration-chart").count():
        page.click("#tab-categorization")
        page.wait_for_function(
            "() => window.Chart && document.querySelector('#calibration-chart').width > 0"
        )
        page.wait_for_timeout(1200)
        page.screenshot(path=OUT / "10a-eval-categorization.png", full_page=True)
    page.click("#tab-fx")
    page.screenshot(path=OUT / "10b-eval-fx.png", full_page=True)
    page.click("#tab-compare")
    page.click("#panel-compare button:has-text('Compare')")
    page.wait_for_timeout(800)
    page.screenshot(path=OUT / "10c-eval-compare.png", full_page=True)
    page.click("#tab-failures")
    page.wait_for_timeout(500)
    page = user_page

    # Grouped sidebar: open "Money" and follow a child link; the group stays open there
    page.goto(f"{BASE}/")
    side = page.locator("aside nav[aria-label=Main]").last
    side.locator("summary", has_text="Money").click()
    side.locator("a", has_text="Recurring").click()
    page.wait_for_url("**/app/recurring")
    expect(page.locator("aside a[aria-current=page]").last).to_have_text("Recurring")
    # Mobile: the sidebar becomes a drawer
    page.set_viewport_size({"width": 390, "height": 844})
    page.click("button[aria-label='Open menu']")
    drawer = page.locator("[role=dialog][aria-label=Navigation]")
    expect(drawer).to_be_visible()
    page.screenshot(path=OUT / "20-mobile-drawer.png")
    drawer.locator("a", has_text="Overview").click()
    page.wait_for_url(f"{BASE}/")
    page.set_viewport_size({"width": 1280, "height": 900})

    # Settings + dark mode
    page.goto(f"{BASE}/account")
    page.click("[data-theme-toggle]")
    page.goto(f"{BASE}/")
    page.screenshot(path=OUT / "09-dashboard-dark.png", full_page=True)
    b.close()

real = [e for e in errors if "favicon" not in e]
print("EMAIL", email)
print("ERRORS", len(real))
for e in real:
    print(" ", e)
sys.exit(1 if real else 0)
