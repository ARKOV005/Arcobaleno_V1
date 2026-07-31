from __future__ import annotations

import hmac
import secrets
from datetime import timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar, cast

from flask import Flask, abort, request, session

_VIEW = TypeVar("_VIEW", bound=Callable[..., Any])
_LOOPBACK_ADDRESSES = {"127.0.0.1", "::1", "localhost"}


def is_operator_request() -> bool:
    """Return True only for requests coming from the event laptop itself."""

    return str(request.remote_addr or "").casefold() in _LOOPBACK_ADDRESSES


def operator_only(view: _VIEW) -> _VIEW:
    """Restrict operator controls to the laptop running ARCOBALENO."""

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        if not is_operator_request():
            abort(403)
        return view(*args, **kwargs)

    return cast(_VIEW, wrapped)


def _load_or_create_secret(runtime_root: Path) -> str:
    secret_path = runtime_root / ".arcobaleno_secret"
    try:
        existing = secret_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""

    if existing:
        return existing

    secret = secrets.token_urlsafe(48)
    secret_path.write_text(secret + "\n", encoding="utf-8")
    return secret


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def initialize_security(application: Flask, runtime_root: Path) -> None:
    application.secret_key = _load_or_create_secret(runtime_root)
    application.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        # Lax is deliberate: QR scans are top-level navigations from the phone's
        # camera/scanner app. Strict cookies are omitted on that navigation, which
        # previously created a fresh guest identity and reset the shot allowance.
        # Lax still blocks cross-site subrequests and state-changing POSTs remain
        # protected by ARCOBALENO's CSRF token.
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
        PERMANENT_SESSION_LIFETIME=timedelta(days=365),
    )

    @application.before_request
    def protect_state_changes():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None

        expected = session.get("_csrf_token")
        supplied = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
        if not isinstance(expected, str) or not isinstance(supplied, str):
            abort(400)
        if not hmac.compare_digest(expected, supplied):
            abort(400)
        return None

    @application.context_processor
    def inject_security_helpers():
        return {
            "csrf_token": _csrf_token,
            "operator_request": is_operator_request(),
        }
