"""Expiry-aware Fyers access-token cache with lazy TOTP refresh.

Fyers access tokens are not renewable: there is no refresh_token grant, and
each one expires at 06:00 IST the morning after it was issued (so a token
minted at 19:00 is dead ~11 hours later, not 24). The only way to get a new
one is to run the whole login flow again.

Before this module the flow was manual: `scripts/fyers_login.py` wrote a
token into `.env` and nothing ever renewed it, so the app kept sending an
expired token and every call came back `-16 Could not authenticate the
user`. This module closes that gap - callers ask for a token, and if the
cached one is expired (or about to be) it silently re-runs the TOTP
auto-login from `broker/fyers_auth.py` and persists the new token.

Refresh needs FYERS_USERNAME/FYERS_TOTP_KEY/FYERS_PIN. Without them there is
nothing to fall back on but the interactive browser flow, which a server
process cannot drive, so we raise a `TokenRefreshError` that says exactly
which script to run by hand.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import set_key

import paths
from broker.errors import AuthFailed
from broker.fyers.auth import AutoLoginError, auto_login
from settings import Settings, load_settings

ENV_PATH = paths.ENV_FILE
ENV_TOKEN_KEY = "FYERS_ACCESS_TOKEN"

# Refresh a little before the real expiry so a request that starts just
# under the wire doesn't land on the far side of it.
REFRESH_SKEW = timedelta(minutes=15)

_refresh_lock = threading.Lock()

# After a failed login, how long before trying again: doubling per failure up
# to the cap. Without it every caller retried the login - the paper watcher
# once a second, every desk request besides - and Fyers answered the login
# endpoint itself with 429, which kept the desk locked out for the morning.
RETRY_AFTER = timedelta(seconds=60)
RETRY_AFTER_MAX = timedelta(minutes=15)

_failures = 0
_failed_until: datetime | None = None
_last_failure: str | None = None


class TokenRefreshError(AuthFailed):
    """Raised when no usable token exists and it cannot be refreshed.

    An `AuthFailed`, so the desk is told to log in (401) rather than handed a 500.
    """


def _decode_payload(token: str) -> dict[str, object] | None:
    """Best-effort peek at a JWT's payload, without verifying the signature."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def jwt_subject(token: str) -> str | None:
    """The JWT's `sub` claim.

    Fyers issues JWTs at two stages: the auth_code (sub="auth_code") and the
    real access_token (sub="access_token"). Used to catch, with a clear
    error, the case where the exchange step didn't actually happen and we're
    about to save the unexchanged auth_code as if it were the token.
    """
    payload = _decode_payload(token)
    subject = payload.get("sub") if payload else None
    return subject if isinstance(subject, str) else None


def token_expiry(token: str) -> datetime | None:
    """The token's expiry as an aware UTC datetime, or None if unreadable."""
    payload = _decode_payload(token)
    exp = payload.get("exp") if payload else None
    if not isinstance(exp, int | float):
        return None
    return datetime.fromtimestamp(exp, UTC)


def token_is_usable(
    token: str, *, now: datetime | None = None, skew: timedelta = REFRESH_SKEW
) -> bool:
    """True if `token` is a real access token with time left on the clock.

    A token whose expiry we cannot read is treated as unusable: better to
    spend one extra login than to serve a whole session's calls with
    something we can't vouch for.
    """
    if not token or jwt_subject(token) != "access_token":
        return False
    expiry = token_expiry(token)
    if expiry is None:
        return False
    return expiry - skew > (now or datetime.now(UTC))


def persist_token(token: str, *, env_path: Path | str = ENV_PATH) -> None:
    """Write the token to `.env` and to this process's environment.

    Updating `os.environ` matters as much as the file: pydantic-settings
    reads real environment variables ahead of `.env`, so a stale value left
    in the environment would otherwise shadow the token we just minted.
    """
    set_key(str(env_path), ENV_TOKEN_KEY, token)
    os.environ[ENV_TOKEN_KEY] = token


def refresh_token(settings: Settings, *, env_path: Path | str = ENV_PATH) -> str:
    """Run the TOTP auto-login flow and persist the resulting token."""
    if not settings.has_auto_login_credentials:
        raise TokenRefreshError(
            "Fyers access token is missing or expired and cannot be refreshed "
            "automatically: FYERS_USERNAME, FYERS_TOTP_KEY and FYERS_PIN are not all "
            "set in .env. Set them (see docs/SETUP.md) or run "
            "`uv run python scripts/fyers_login.py` by hand."
        )
    try:
        token = auto_login(
            client_id=settings.fyers_client_id,
            secret_key=settings.fyers_secret_key,
            redirect_uri=settings.fyers_redirect_uri,
            fy_id=settings.fyers_username,
            totp_key=settings.fyers_totp_key,
            pin=settings.fyers_pin,
        )
    except AutoLoginError as exc:
        raise TokenRefreshError(
            f"TOTP auto-login failed while refreshing the Fyers access token: {exc}. "
            "Run `uv run python scripts/fyers_login.py` to log in manually."
        ) from exc

    if jwt_subject(token) != "access_token":
        raise TokenRefreshError(
            "Auto-login returned something that isn't an access token "
            f"(sub={jwt_subject(token)!r}); not saving it."
        )
    persist_token(token, env_path=env_path)
    return token


def get_access_token(
    settings: Settings | None = None,
    *,
    env_path: Path | str = ENV_PATH,
    force: bool = False,
    now: datetime | None = None,
) -> str:
    """A usable Fyers access token, refreshing it first if need be.

    Serialised on a lock so that several concurrent requests arriving after
    expiry trigger one login between them rather than one each; whoever gets
    the lock second sees the fresh token and returns it.
    """
    settings = settings or load_settings()
    if not force and token_is_usable(settings.fyers_access_token, now=now):
        return settings.fyers_access_token

    global _failures, _failed_until, _last_failure
    at = now or datetime.now(UTC)
    with _refresh_lock:
        # Re-read: another thread may have refreshed while we waited.
        current = os.environ.get(ENV_TOKEN_KEY, settings.fyers_access_token)
        if not force and token_is_usable(current, now=now):
            return current
        # A login just failed: say so again rather than asking Fyers again.
        if not force and _failed_until is not None and at < _failed_until:
            wait = int((_failed_until - at).total_seconds()) + 1
            raise TokenRefreshError(f"{_last_failure} Trying again in {wait}s.")
        try:
            token = refresh_token(settings, env_path=env_path)
        except TokenRefreshError as exc:
            _failures += 1
            _failed_until = at + min(RETRY_AFTER * 2 ** (_failures - 1), RETRY_AFTER_MAX)
            _last_failure = str(exc)
            raise
        _failures, _failed_until, _last_failure = 0, None, None
        return token


def reset_refresh_backoff() -> None:
    """Forget earlier login failures, so the next call tries straight away."""
    global _failures, _failed_until, _last_failure
    with _refresh_lock:
        _failures, _failed_until, _last_failure = 0, None, None
