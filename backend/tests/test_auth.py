"""auth.py: token lifecycle, retries, and above all no credential leakage."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import SecretStr

from app.auth import AuthError, Token, TokenProvider
from app.config import Settings

USER = "test-user@example.invalid"
PASSWORD = "test-password-placeholder"
TOKEN_VALUE = "test-token-placeholder"


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "openf1_username": SecretStr(USER),
        "openf1_password": SecretStr(PASSWORD),
        "live_mode": True,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


class Recorder:
    """Captures the requests a MockTransport sees."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []


def provider_with(
    handler: "callable[[httpx.Request], httpx.Response]",
    *,
    settings: Settings | None = None,
    **kwargs: object,
) -> tuple[TokenProvider, list[float]]:
    """A TokenProvider wired to a mock transport and a non-sleeping sleep."""
    slept: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = TokenProvider(
        settings or make_settings(),
        client=client,
        sleep=slept.append,
        **kwargs,  # type: ignore[arg-type]
    )
    return provider, slept


def ok_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": TOKEN_VALUE, "token_type": "bearer", "expires_in": 3600},
    )


# -- happy path -------------------------------------------------------------


def test_posts_form_encoded_credentials_to_the_documented_url() -> None:
    seen = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.requests.append(request)
        return ok_response(request)

    provider, _ = provider_with(handler)
    assert provider.get_token() == TOKEN_VALUE

    assert len(seen.requests) == 1
    request = seen.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.openf1.org/token"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    body = request.content.decode()
    assert "username=" in body and "password=" in body


def test_expiry_is_derived_from_expires_in() -> None:
    provider, _ = provider_with(ok_response)
    before = datetime.now(timezone.utc)
    provider.get_token()
    expiry = provider.current_expiry
    assert expiry is not None
    # 3600s from the moment of the request, give or take test execution time.
    assert timedelta(seconds=3590) <= (expiry - before) <= timedelta(seconds=3610)


def test_missing_expires_in_defaults_to_one_hour() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": TOKEN_VALUE})

    provider, _ = provider_with(handler)
    provider.get_token()
    assert provider.current_expiry is not None
    assert provider.seconds_until_refresh() > 3000


# -- caching and refresh ----------------------------------------------------


def test_token_is_cached_between_calls() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return ok_response(request)

    provider, _ = provider_with(handler)
    provider.get_token()
    provider.get_token()
    provider.get_token()
    assert calls["n"] == 1, "a valid cached token must not trigger a new request"


def test_force_refresh_bypasses_the_cache() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return ok_response(request)

    provider, _ = provider_with(handler)
    provider.get_token()
    provider.get_token(force_refresh=True)
    assert calls["n"] == 2


def test_token_inside_the_refresh_margin_is_replaced() -> None:
    """A token with 3600s left and a 4000s margin is already 'stale'."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return ok_response(request)

    provider, _ = provider_with(handler, refresh_margin_seconds=4000)
    provider.get_token()
    provider.get_token()
    assert calls["n"] == 2
    assert provider.seconds_until_refresh() == 0.0


def test_is_stale_boundary() -> None:
    now = datetime.now(timezone.utc)
    token = Token("x", now + timedelta(seconds=300))
    assert token.is_stale(300, now) is True
    assert token.is_stale(299, now) is False


# -- failures ---------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403])
def test_rejected_credentials_fail_fast_with_a_helpful_message(status: int) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, json={"detail": "nope"})

    provider, slept = provider_with(handler, max_attempts=3)
    with pytest.raises(AuthError) as excinfo:
        provider.get_token()

    message = str(excinfo.value)
    assert "OPENF1_USERNAME" in message and "OPENF1_PASSWORD" in message
    assert str(status) in message
    # Retries still happen (the endpoint could be flapping), but the message
    # tells the developer the likely cause.
    assert calls["n"] == 3
    assert slept == [2.0, 4.0]


def test_server_error_retries_with_capped_exponential_backoff() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    provider, slept = provider_with(handler, max_attempts=6)
    with pytest.raises(AuthError):
        provider.get_token()

    assert slept == [2.0, 4.0, 8.0, 16.0, 30.0], "backoff must double then cap at 30s"


def test_recovers_once_the_endpoint_comes_back() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return ok_response(request)

    provider, slept = provider_with(handler)
    assert provider.get_token() == TOKEN_VALUE
    assert calls["n"] == 3
    assert slept == [2.0, 4.0]


def test_network_error_is_wrapped_without_echoing_the_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider, _ = provider_with(handler, max_attempts=1)
    with pytest.raises(AuthError) as excinfo:
        provider.get_token()
    assert "ConnectError" in str(excinfo.value)


def test_malformed_responses_are_rejected() -> None:
    for body in ({"token_type": "bearer"}, {"access_token": ""}, {"access_token": 42}):
        provider, _ = provider_with(
            lambda request, b=body: httpx.Response(200, json=b), max_attempts=1
        )
        with pytest.raises(AuthError, match="access_token"):
            provider.get_token()

    provider, _ = provider_with(
        lambda request: httpx.Response(200, text="<html>nope</html>"), max_attempts=1
    )
    with pytest.raises(AuthError, match="non-JSON"):
        provider.get_token()


def test_missing_credentials_are_caught_before_any_request() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return ok_response(request)

    provider, _ = provider_with(handler, settings=make_settings(openf1_password=SecretStr("")))
    with pytest.raises(Exception) as excinfo:
        provider.get_token()
    assert "OPENF1_PASSWORD" in str(excinfo.value)
    assert calls["n"] == 0, "must not hit the network with empty credentials"


# -- the leak tests ---------------------------------------------------------


@pytest.mark.parametrize("status", [200, 401, 500])
def test_no_credential_or_token_ever_appears_in_errors_or_reprs(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if status == 200:
            return ok_response(request)
        # A hostile endpoint that echoes the credentials back at us.
        return httpx.Response(status, json={"echo": {"user": USER, "pass": PASSWORD}})

    provider, _ = provider_with(handler, max_attempts=1)
    try:
        provider.get_token()
        rendered = ""
    except AuthError as exc:
        rendered = str(exc) + repr(exc)

    surfaces = [rendered, repr(provider), str(provider.current_expiry)]
    token = provider._token  # noqa: SLF001 - asserting on the redacted repr
    if token is not None:
        surfaces.extend([repr(token), str(token), json.dumps(str(token))])

    for surface in surfaces:
        assert PASSWORD not in surface
        assert USER not in surface
        assert TOKEN_VALUE not in surface
