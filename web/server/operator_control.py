"""Shared authorization boundary for operator-only HTTP mutations.

Read-only dashboard routes deliberately remain public.  A state-changing route
must call :func:`require_operator_mutation` before it reads or mutates runtime
state.  This keeps the default local dashboard convenient without turning a
``0.0.0.0`` bind plus permissive CORS into a remote control API.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


CONTROL_TOKEN_ENV = "POK_CONTROL_TOKEN"
CONTROL_TOKEN_HEADER = "X-Control-Token"


def _is_loopback(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def _is_loopback_authority(host: str) -> bool:
    """Reject DNS-rebinding Host values on the tokenless local path."""

    try:
        parsed = urlsplit(f"//{host}")
        hostname = (parsed.hostname or "").rstrip(".").lower()
        # Accessing ``port`` also rejects malformed/non-numeric port syntax.
        _ = parsed.port
    except ValueError:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    return hostname == "localhost" or _is_loopback(hostname)


def _same_origin(request: Request) -> bool:
    """Require an explicit, syntactically valid Origin matching this request."""

    origin = request.headers.get("origin", "").strip()
    host = request.headers.get("host", "").strip().lower()
    if not origin or not host or not _is_loopback_authority(host):
        return False
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return False
    return (
        parsed.scheme.lower() == request.url.scheme.lower()
        and parsed.netloc.lower() == host
    )


def require_operator_mutation(
    request: Request,
    *,
    operation: str,
    token_env: str = CONTROL_TOKEN_ENV,
    token_header: str = CONTROL_TOKEN_HEADER,
) -> None:
    """Authorize one HTTP mutation or fail closed with ``403``.

    A correctly configured token authorizes explicit remote automation.  The
    token is compared in constant time.  Without it, only a loopback client
    with an explicit same-origin ``Origin`` header is accepted.  Requiring both
    properties prevents a permissive CORS policy or a forged Host/Origin pair
    from granting a remote caller operator authority.
    """

    configured = os.environ.get(token_env, "")
    supplied = request.headers.get(token_header, "")
    if configured and supplied and hmac.compare_digest(configured, supplied):
        return

    client_host = request.client.host if request.client else ""
    if _is_loopback(client_host) and _same_origin(request):
        return

    if configured:
        detail = "operator mutation requires a valid control token or loopback same-origin request"
    else:
        detail = "remote or cross-origin operator mutation requires POK_CONTROL_TOKEN"
    raise HTTPException(
        status_code=403,
        detail={
            "code": "operator_control_forbidden",
            "operation": operation,
            "message": detail,
        },
    )


__all__ = [
    "CONTROL_TOKEN_ENV",
    "CONTROL_TOKEN_HEADER",
    "require_operator_mutation",
]
