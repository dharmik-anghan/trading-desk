"""Tests for the expiry-aware token cache.

The bug this module exists to prevent: a token was written to `.env` once
and never renewed, so the day after login every Fyers call came back `-16
Could not authenticate the user`. These tests pin the two halves of the fix
- recognising that a token has expired, and refreshing it exactly once when
it has.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from broker.fyers import token_store
from broker.fyers.token_store import (
    TokenRefreshError,
    get_access_token,
    jwt_subject,
    token_expiry,
    token_is_usable,
)
from settings import Settings

NOW = datetime(2026, 9, 21, 4, 0, tzinfo=UTC)


def make_token(*, sub: str = "access_token", exp: datetime | None = None) -> str:
    """A JWT-shaped string with a readable payload and a junk signature.

    Nothing in this codebase verifies Fyers' signature (we only read claims),
    so an unsigned payload is a faithful stand-in.
    """
    payload: dict[str, object] = {"sub": sub}
    if exp is not None:
        payload["exp"] = int(exp.timestamp())
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def make_settings(token: str, *, auto_login: bool = True) -> Settings:
    return Settings(
        fyers_client_id="ABCD-100",
        fyers_secret_key="secret",
        fyers_redirect_uri="https://127.0.0.1",
        fyers_access_token=token,
        fyers_username="XY12345" if auto_login else "",
        fyers_totp_key="JBSWY3DPEHPK3PXP" if auto_login else "",
        fyers_pin="1234" if auto_login else "",
    )


@pytest.fixture(autouse=True)
def _isolate_env() -> Iterator[None]:
    """Keep the process environment out of (and unchanged by) these tests.

    `persist_token` writes the refreshed token into `os.environ` on purpose,
    so without this a leaked value would break any later test that asserts on
    a clean environment.
    """
    previous = os.environ.get(token_store.ENV_TOKEN_KEY)
    os.environ.pop(token_store.ENV_TOKEN_KEY, None)
    token_store.reset_refresh_backoff()
    try:
        yield
    finally:
        token_store.reset_refresh_backoff()
        if previous is None:
            os.environ.pop(token_store.ENV_TOKEN_KEY, None)
        else:
            os.environ[token_store.ENV_TOKEN_KEY] = previous


def test_token_expiry_reads_exp_claim() -> None:
    expiry = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)
    assert token_expiry(make_token(exp=expiry)) == expiry


def test_jwt_subject_distinguishes_auth_code_from_access_token() -> None:
    assert jwt_subject(make_token(sub="access_token")) == "access_token"
    assert jwt_subject(make_token(sub="auth_code")) == "auth_code"


def test_jwt_subject_returns_none_for_unreadable_tokens() -> None:
    assert jwt_subject("not-a-jwt") is None
    assert jwt_subject(make_token(sub="")) == ""
    assert jwt_subject("header.!!!notbase64!!!.signature") is None


def test_token_with_time_left_is_usable() -> None:
    assert token_is_usable(make_token(exp=NOW + timedelta(hours=2)), now=NOW)


def test_expired_token_is_not_usable() -> None:
    assert not token_is_usable(make_token(exp=NOW - timedelta(minutes=1)), now=NOW)


def test_token_inside_refresh_skew_is_not_usable() -> None:
    """The real failure was a token that died mid-session, so refresh early."""
    assert not token_is_usable(make_token(exp=NOW + timedelta(minutes=5)), now=NOW)


def test_token_without_readable_expiry_is_not_usable() -> None:
    assert not token_is_usable(make_token(), now=NOW)
    assert not token_is_usable("", now=NOW)


def test_auth_code_saved_as_token_is_not_usable() -> None:
    token = make_token(sub="auth_code", exp=NOW + timedelta(hours=2))
    assert not token_is_usable(token, now=NOW)


def test_valid_token_is_returned_without_refreshing(monkeypatch: pytest.MonkeyPatch) -> None:
    token = make_token(exp=NOW + timedelta(hours=2))

    def fail(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("should not refresh a token that is still good")

    monkeypatch.setattr(token_store, "auto_login", fail)
    assert get_access_token(make_settings(token), now=NOW) == token


def test_expired_token_triggers_refresh_and_is_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fresh = make_token(exp=NOW + timedelta(hours=14))
    calls: list[dict[str, object]] = []

    def fake_auto_login(**kwargs: object) -> str:
        calls.append(kwargs)
        return fresh

    monkeypatch.setattr(token_store, "auto_login", fake_auto_login)
    env_path = tmp_path / ".env"
    env_path.write_text("FYERS_ACCESS_TOKEN=stale\n")

    expired = make_token(exp=NOW - timedelta(hours=1))
    returned = get_access_token(make_settings(expired), env_path=env_path, now=NOW)

    assert returned == fresh
    assert len(calls) == 1
    assert calls[0]["fy_id"] == "XY12345"
    assert fresh in env_path.read_text()


def test_refresh_without_credentials_explains_the_manual_step(tmp_path: Path) -> None:
    expired = make_token(exp=NOW - timedelta(hours=1))
    settings = make_settings(expired, auto_login=False)

    with pytest.raises(TokenRefreshError, match="fyers_login.py"):
        get_access_token(settings, env_path=tmp_path / ".env", now=NOW)


def test_refresh_rejects_an_unexchanged_auth_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Guards the same mistake `fyers_login.py` already refuses to make."""
    monkeypatch.setattr(
        token_store,
        "auto_login",
        lambda **_kwargs: make_token(sub="auth_code", exp=NOW + timedelta(hours=2)),
    )
    env_path = tmp_path / ".env"

    with pytest.raises(TokenRefreshError, match="auth_code"):
        get_access_token(
            make_settings(make_token(exp=NOW - timedelta(hours=1))),
            env_path=env_path,
            now=NOW,
        )
    assert not env_path.exists()


def test_failed_login_is_not_retried_until_the_cooldown_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every caller retrying a failed login is what got the login endpoint itself
    rate limited: the paper watcher alone asked once a second."""
    from broker.fyers.auth import AutoLoginError

    calls: list[datetime] = []

    def refused(**_kwargs: object) -> str:
        calls.append(NOW)
        raise AutoLoginError("429 Client Error: Too Many Requests")

    monkeypatch.setattr(token_store, "auto_login", refused)
    settings = make_settings(make_token(exp=NOW - timedelta(hours=1)))
    env_path = tmp_path / ".env"

    with pytest.raises(TokenRefreshError, match="Too Many Requests"):
        get_access_token(settings, env_path=env_path, now=NOW)
    with pytest.raises(TokenRefreshError, match="Trying again in"):
        get_access_token(settings, env_path=env_path, now=NOW + timedelta(seconds=30))
    assert len(calls) == 1

    # After the first cooldown it tries again, and a second failure waits longer.
    with pytest.raises(TokenRefreshError):
        get_access_token(settings, env_path=env_path, now=NOW + timedelta(seconds=61))
    assert len(calls) == 2
    with pytest.raises(TokenRefreshError, match="Trying again in"):
        get_access_token(settings, env_path=env_path, now=NOW + timedelta(seconds=61 + 90))
    assert len(calls) == 2


def test_a_successful_login_clears_the_cooldown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from broker.fyers.auth import AutoLoginError

    fresh = make_token(exp=NOW + timedelta(hours=14))
    answers: list[str | Exception] = [AutoLoginError("down"), fresh]

    def flaky(**_kwargs: object) -> str:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(token_store, "auto_login", flaky)
    settings = make_settings(make_token(exp=NOW - timedelta(hours=1)))
    env_path = tmp_path / ".env"

    with pytest.raises(TokenRefreshError):
        get_access_token(settings, env_path=env_path, now=NOW)
    assert get_access_token(settings, env_path=env_path, now=NOW + timedelta(seconds=61)) == fresh
    assert token_store._failed_until is None


def test_a_refresh_failure_is_an_auth_failure_for_the_desk() -> None:
    """So the API answers 401 "log in" rather than a 500 with a traceback."""
    from broker.errors import AuthFailed

    assert issubclass(TokenRefreshError, AuthFailed)
