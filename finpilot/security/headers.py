"""Security headers. CSP allows only our origin plus the pinned CDNs used by the UI.

Notes: Alpine.js (standard build) evaluates expressions with `new Function` → 'unsafe-eval';
the Tailwind Play CDN injects a <style> element → style-src 'unsafe-inline'. The Inter font
(@fontsource-variable/inter, pinned) is served by jsDelivr → style-src/font-src allow it. No inline
<script> is used anywhere (all JS lives in /static), so script-src has no 'unsafe-inline'.
"""

from __future__ import annotations

CDN = "https://cdn.jsdelivr.net https://cdn.tailwindcss.com"


def security_headers(is_prod: bool) -> dict[str, str]:
    csp = "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' 'unsafe-eval' {CDN}",
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
            "img-src 'self' data:",
            "font-src 'self' data: https://cdn.jsdelivr.net",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
        ]
    )
    h = {
        "Content-Security-Policy": csp,
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Cross-Origin-Opener-Policy": "same-origin",
    }
    if is_prod:
        h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return h
