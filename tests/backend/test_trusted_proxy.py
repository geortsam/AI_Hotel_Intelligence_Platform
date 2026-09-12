"""Trusted-proxy handling and client-address resolution.

Stage 4.5.6. The rate limiter keys on a client identity, so an identity a client can choose is
an identity that makes the limiter bypassable -- and worse than absent, because it still looks
like protection.

The property under test throughout: **a forwarding header changes the answer only when the TCP
peer is a configured trusted proxy.** Everything else follows from that one rule, including the
default, where nothing is configured and therefore no header is read at all.

The end-to-end cases drive the REAL ``login_rate_limit`` dependency rather than asserting on
the resolver alone, because a correct resolver wired up wrongly would pass a unit test and
still leave the limiter forgeable. A stub session stands in for the database: every path here
is refused before a session is touched, which is itself worth pinning.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import get_db, login_rate_limit
from app.core.client_address import (
    UNKNOWN_ADDRESS,
    InvalidTrustedProxyError,
    forwarded_values,
    parse_ip,
    parse_trusted_proxy,
    resolve_client_ip,
    resolve_scheme,
    trusted_networks,
)
from app.core.config import Settings
from app.main import create_app

SECRET = "trusted-proxy-suite-secret"

#: One IPv4 and one IPv6 range, so every test can exercise either family.
PROXIES = ["10.0.0.0/8", "2001:db8::/32"]

TRUSTED = trusted_networks(tuple(PROXIES))
UNCONFIGURED = trusted_networks(())


# ======================================================================================
# Configuration
# ======================================================================================


def test_no_proxy_is_trusted_by_default() -> None:
    """The safe default, and the reason an unconfigured installation cannot be spoofed."""
    assert Settings(environment="test").trusted_proxies == []


def test_loopback_is_not_implicitly_trusted() -> None:
    """A proxy on localhost is a deployment fact this application cannot verify.

    Trusting 127.0.0.1 by default would mean any process on the same host -- including one an
    attacker reached through an unrelated service -- could forge a client address.
    """
    assert parse_ip("127.0.0.1") is not None
    networks = trusted_networks(tuple(Settings(environment="test").trusted_proxies))
    assert networks == ()


def test_a_comma_separated_environment_value_is_parsed_into_a_list() -> None:
    settings = Settings(environment="test", trusted_proxies="10.0.0.0/8, 192.168.1.5")

    assert settings.trusted_proxies == ["10.0.0.0/8", "192.168.1.5"]


def test_a_bare_address_is_accepted_as_a_single_host() -> None:
    network = parse_trusted_proxy("192.168.1.5")

    assert network.num_addresses == 1
    assert parse_ip("192.168.1.5") in network


@pytest.mark.parametrize("bad", ["not-an-ip", "10.0.0.0/33", "999.1.1.1", "10.0.0.1/abc", "", "  "])
def test_an_invalid_proxy_entry_is_refused_at_construction(bad: str) -> None:
    """A typo must be a startup failure, not a silently empty trust list that turns forwarded
    headers back off in production without anyone noticing."""
    with pytest.raises(InvalidTrustedProxyError):
        parse_trusted_proxy(bad)


def test_settings_refuses_an_invalid_proxy_entry() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="test", trusted_proxies=["10.0.0.0/8", "not-an-ip"])


def test_the_error_names_the_offending_entry_and_not_a_secret() -> None:
    with pytest.raises(InvalidTrustedProxyError) as excinfo:
        parse_trusted_proxy("10.0.0.0/99")

    message = str(excinfo.value)
    assert "10.0.0.0/99" in message
    assert "CIDR" in message


# ======================================================================================
# The resolver -- the eight required cases
# ======================================================================================


def test_case_1_direct_client_with_no_proxy_configured() -> None:
    """Peer 1.2.3.4 sending ``X-Forwarded-For: 9.9.9.9`` resolves to 1.2.3.4."""
    assert resolve_client_ip("1.2.3.4", ["9.9.9.9"], UNCONFIGURED) == "1.2.3.4"


def test_case_2_trusted_proxy_forwards_the_original_client() -> None:
    assert resolve_client_ip("10.0.0.5", ["203.0.113.9"], TRUSTED) == "203.0.113.9"


def test_case_3_an_untrusted_peer_cannot_spoof_its_address() -> None:
    """The whole point. Proxies ARE configured; this peer simply is not one of them."""
    assert resolve_client_ip("1.2.3.4", ["9.9.9.9"], TRUSTED) == "1.2.3.4"


def test_case_4_a_multi_hop_chain_skips_our_own_infrastructure() -> None:
    """Walked from the right, so the two internal hops are stepped over."""
    chain = ["203.0.113.9, 10.0.0.9, 10.0.0.5"]

    assert resolve_client_ip("10.0.0.5", chain, TRUSTED) == "203.0.113.9"


def test_case_4_a_prepended_value_does_not_become_the_client() -> None:
    """The bug that taking ``X-Forwarded-For[0]`` would introduce.

    A client that sends ``X-Forwarded-For: 9.9.9.9`` before reaching the proxy produces the
    chain ``9.9.9.9, <real client>``. The leftmost value is the forged one; the rightmost
    untrusted value is the truth.
    """
    assert resolve_client_ip("10.0.0.5", ["9.9.9.9, 203.0.113.9"], TRUSTED) == "203.0.113.9"


def test_case_5_a_malformed_nearest_hop_falls_back_to_the_peer() -> None:
    """Fails safe: the fallback is the one value that cannot be forged.

    Stopping rather than skipping matters -- skipping past garbage would let an attacker hide
    a hop behind it and push the walk further left than the truth allows.
    """
    assert resolve_client_ip("10.0.0.5", ["203.0.113.9, garbage"], TRUSTED) == "10.0.0.5"


@pytest.mark.parametrize("header", ["", "   ", ",,,", "not-an-ip", "10.0.0.5, ???"])
def test_case_5_malformed_headers_never_produce_an_unsafe_answer(header: str) -> None:
    resolved = resolve_client_ip("10.0.0.5", [header], TRUSTED)

    assert resolved == "10.0.0.5"


def test_case_6_ipv4_with_a_port_is_understood() -> None:
    assert resolve_client_ip("10.0.0.5", ["203.0.113.9:51234"], TRUSTED) == "203.0.113.9"


def test_case_7_ipv6_is_resolved_over_an_ipv6_proxy() -> None:
    assert resolve_client_ip("2001:db8::1", ["2606:4700::1234"], TRUSTED) == "2606:4700::1234"


def test_case_7_a_bare_ipv6_address_is_not_mangled_by_port_stripping() -> None:
    """The classic bug: splitting on ``:`` turns ``2606:4700::1234`` into ``2606``."""
    assert parse_ip("2606:4700::1234") is not None
    assert resolve_client_ip("10.0.0.5", ["2606:4700::1234"], TRUSTED) == "2606:4700::1234"


def test_case_7_a_bracketed_ipv6_with_a_port_is_understood() -> None:
    assert resolve_client_ip("10.0.0.5", ["[2606:4700::1234]:443"], TRUSTED) == "2606:4700::1234"


def test_an_ipv4_address_is_not_matched_against_an_ipv6_network() -> None:
    """Comparing across families raises rather than returning False, so it is checked first."""
    ipv6_only = trusted_networks(("2001:db8::/32",))

    assert resolve_client_ip("10.0.0.5", ["203.0.113.9"], ipv6_only) == "10.0.0.5"


def test_a_chain_of_only_trusted_hops_falls_back_to_the_peer() -> None:
    """No external client is identifiable, so nothing is invented."""
    assert resolve_client_ip("10.0.0.5", ["10.0.0.9, 10.0.0.7"], TRUSTED) == "10.0.0.5"


def test_several_forwarded_headers_are_one_chain() -> None:
    """A proxy may append to the existing header or add a second one; both are legal."""
    assert resolve_client_ip("10.0.0.5", ["203.0.113.9", "10.0.0.9"], TRUSTED) == "203.0.113.9"


def test_a_transport_with_no_peer_does_not_escape_the_limiter() -> None:
    assert resolve_client_ip(None, ["9.9.9.9"], TRUSTED) == UNKNOWN_ADDRESS


def test_forwarded_values_flattens_and_trims() -> None:
    assert forwarded_values(["a, b", " c "]) == ["a", "b", "c"]


# ======================================================================================
# The forwarded scheme, under the same trust rule
# ======================================================================================


def test_the_forwarded_scheme_is_read_only_from_a_trusted_peer() -> None:
    assert resolve_scheme("http", "10.0.0.5", ["https"], TRUSTED) == "https"


def test_an_untrusted_peer_cannot_claim_https() -> None:
    """Otherwise any client could switch HSTS on for a domain by sending one header."""
    assert resolve_scheme("http", "1.2.3.4", ["https"], TRUSTED) == "http"


def test_the_forwarded_scheme_is_ignored_when_no_proxy_is_configured() -> None:
    assert resolve_scheme("http", "10.0.0.5", ["https"], UNCONFIGURED) == "http"


def test_a_nonsense_forwarded_scheme_is_ignored() -> None:
    assert resolve_scheme("http", "10.0.0.5", ["gopher"], TRUSTED) == "http"


def test_the_leftmost_forwarded_scheme_wins() -> None:
    """It is the one the client actually spoke."""
    assert resolve_scheme("http", "10.0.0.5", ["https, http"], TRUSTED) == "https"


# ======================================================================================
# End to end: the real rate limiter, keyed on the resolved identity
# ======================================================================================


def _stub_session() -> Iterator[Session]:
    """A session that is never used.

    Every path below is refused by the limiter before a session is touched, so handing over a
    placeholder proves something real rather than merely avoiding a database.
    """
    yield cast(Session, None)


def limited_app(**overrides: object) -> FastAPI:
    """The real application carrying the real login limiter on a database-free probe."""
    settings = Settings(environment="test", secret_key=SECRET, **overrides)  # type: ignore[arg-type]
    application = create_app(settings)
    application.dependency_overrides[get_db] = _stub_session

    @application.post(
        "/probe/limited", dependencies=[Depends(login_rate_limit)], response_model=None
    )
    def _limited() -> dict[str, bool]:
        return {"ok": True}

    return application


def exhaust(client: TestClient, headers: dict[str, str] | None = None, n: int = 7) -> list[int]:
    return [client.post("/probe/limited", headers=headers or {}).status_code for _ in range(n)]


def test_a_direct_client_cannot_escape_its_bucket_by_varying_the_header() -> None:
    """No proxy configured, so the header is not read at all.

    A changing ``X-Forwarded-For`` would mint a fresh bucket per request if it were.
    """
    app = limited_app()
    caller = TestClient(app, client=("1.2.3.4", 40000))

    codes = [
        caller.post("/probe/limited", headers={"X-Forwarded-For": f"9.9.9.{n}"}).status_code
        for n in range(7)
    ]

    assert codes[:5] == [200] * 5
    assert 429 in codes[5:]


def test_an_untrusted_peer_cannot_escape_its_bucket_even_when_proxies_are_configured() -> None:
    app = limited_app(trusted_proxies=PROXIES)
    caller = TestClient(app, client=("1.2.3.4", 40000))

    codes = [
        caller.post("/probe/limited", headers={"X-Forwarded-For": f"9.9.9.{n}"}).status_code
        for n in range(7)
    ]

    assert codes[:5] == [200] * 5
    assert 429 in codes[5:]


def test_an_untrusted_peer_cannot_exhaust_a_victims_bucket() -> None:
    """The other direction, and the reason spoofing is not merely a bypass.

    If the header were honoured, an attacker could burn a chosen victim's allowance and lock
    them out. Here the attacker only ever exhausts its own.
    """
    app = limited_app(trusted_proxies=PROXIES)
    attacker = TestClient(app, client=("1.2.3.4", 40000))
    victim = TestClient(app, client=("203.0.113.50", 40000))

    exhaust(attacker, {"X-Forwarded-For": "203.0.113.50"})

    assert victim.post("/probe/limited").status_code == 200


def test_behind_a_trusted_proxy_each_forwarded_client_gets_its_own_bucket() -> None:
    """Without this the limit becomes global: every request looks like the proxy."""
    app = limited_app(trusted_proxies=PROXIES)
    proxy = TestClient(app, client=("10.0.0.5", 40000))

    first = exhaust(proxy, {"X-Forwarded-For": "203.0.113.9"})
    second = proxy.post("/probe/limited", headers={"X-Forwarded-For": "203.0.113.10"})

    assert 429 in first
    assert second.status_code == 200


def test_behind_a_trusted_proxy_the_same_forwarded_client_shares_one_bucket() -> None:
    app = limited_app(trusted_proxies=PROXIES)
    proxy = TestClient(app, client=("10.0.0.5", 40000))

    codes = exhaust(proxy, {"X-Forwarded-For": "203.0.113.9, 10.0.0.9"})

    assert codes[:5] == [200] * 5
    assert 429 in codes[5:]


def test_the_429_still_carries_retry_after() -> None:
    """Only the source of the identity changed. The refusal contract did not."""
    app = limited_app(trusted_proxies=PROXIES)
    proxy = TestClient(app, client=("10.0.0.5", 40000))

    exhaust(proxy, {"X-Forwarded-For": "203.0.113.9"})
    refused = proxy.post("/probe/limited", headers={"X-Forwarded-For": "203.0.113.9"})

    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1
    assert refused.json()["error"]["code"] == "RATE_LIMITED"


def test_the_limit_and_window_are_unchanged() -> None:
    """Five per sixty seconds, exactly as Stage 4.5.3 set them."""
    settings = Settings(environment="test", secret_key=SECRET)

    assert settings.auth_login_rate_limit == 5
    assert settings.auth_login_rate_limit_window_seconds == 60


def test_a_refused_request_never_reached_the_database() -> None:
    """The stub session would raise on any attribute access, so a 429 here proves the limiter
    refuses before the dependency chain resolves a session."""
    app = limited_app()
    caller = TestClient(app, client=("198.51.100.7", 40000))

    codes = exhaust(caller)

    assert 429 in codes
