"""Shared request helpers."""

from __future__ import annotations

from fastapi import Request

from finpilot.auth.service import ClientInfo
from finpilot.security.ratelimit import enforce


def client_info(request: Request) -> ClientInfo:
    ip = request.client.host if request.client else None
    return ClientInfo(ip=ip, user_agent=request.headers.get("user-agent"))


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def limit(
    request: Request, scope: str, limit_n: int, window_s: int, key: str | None = None
) -> None:
    enforce(scope, key or client_ip(request), limit_n, window_s)
