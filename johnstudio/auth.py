"""Loopback bearer-token auth for the local FastAPI server.

JohnStudio binds to 127.0.0.1, but loopback is a shared bus on a multi-user
or compromised machine: any local process (rogue npm package, curl, another
user) can hit the API and trigger task runs, merges, or shell execution.

The defense is a single random token, generated once and stored in
`<JOHNSTUDIO_HOME>/server_token` with 0600 perms. Every `/api/*` request
must carry `Authorization: Bearer <token>` except the unauthenticated
health probe (`/api/health`) which the UI uses to show connection state.

The token is intentionally tied to the server process's filesystem identity:
anything that can read `~/.johnstudio/server_token` is, by construction,
running as the same user — and that's the trust boundary we're enforcing.
"""
from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from fastapi import Header, HTTPException, status

from .config import home_dir


TOKEN_FILENAME = "server_token"

# Paths exempt from auth. Keep this list tiny — health is read by the UI to
# show a connection badge before the user has had a chance to authenticate.
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/api/health",
    "/docs",
    "/openapi.json",
    "/redoc",
})


def token_path() -> Path:
    return home_dir() / TOKEN_FILENAME


def get_or_create_token() -> str:
    """Return the current server token, creating one if absent.

    Token is 32 bytes of secrets.token_hex (256 bits of entropy). File is
    written with 0600 — readable only by the owning user.
    """
    p = token_path()
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    # Write with restrictive perms on platforms that honor them.
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    return token


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def require_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency. Raise 401 if the Bearer token is missing or wrong.

    Constant-time comparison via `hmac.compare_digest` to avoid timing
    side-channels — overkill on localhost but cheap and correct.
    """
    presented = _extract_bearer(authorization)
    expected = get_or_create_token()
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
