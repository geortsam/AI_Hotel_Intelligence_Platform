"""Resolve the address a request really came from.

Rate limiting keys on a client identity. If that identity can be forged the limiter is worse
than absent: it looks like protection while an attacker mints a fresh bucket per request by
varying a header. Everything here exists to make the identity unforgeable.

**The trust boundary is the whole design.** ``X-Forwarded-For`` is appended to by every hop,
including the first one -- the client. Nothing inside the header distinguishes a value written
by your load balancer from a value the client invented before the load balancer ever saw it.
The only fact that cannot be forged is the TCP peer address, so that is what decides whether
any header is read at all.

The algorithm, exactly:

1. No peer address (a transport with no client) -> :data:`UNKNOWN_ADDRESS`. Headers ignored.
2. No trusted proxies configured -> the peer address. Headers ignored. **This is the
   default**, so an installation that never configures a proxy cannot be spoofed.
3. Peer is not inside any trusted network -> the peer address. Headers ignored. This is the
   case that defeats a direct connection sending ``X-Forwarded-For: <victim>``.
4. Peer IS trusted -> walk the chain from the RIGHT, which is the end nearest to us and
   therefore the end written by infrastructure we trust:

   * a valid IP inside a trusted network -> one of our own hops, keep walking left;
   * a valid IP outside every trusted network -> **this is the client**, stop;
   * not a valid IP -> stop and fall back to the peer. A malformed entry means everything
     further left is attacker-influenced, so nothing left of it may be read.

   If the walk runs out without finding an untrusted entry, every hop was our own
   infrastructure and no external client is identifiable: fall back to the peer address.

Taking ``X-Forwarded-For[0]`` instead -- the common shortcut -- is precisely the
vulnerability, because the leftmost value is the one furthest from us and the one the client
controls.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Final

#: Used when the transport has no peer at all. Such requests share one rate-limit bucket
#: rather than escaping the limiter, which is the safe direction.
UNKNOWN_ADDRESS: Final = "unknown"

FORWARDED_FOR_HEADER: Final = "x-forwarded-for"
FORWARDED_PROTO_HEADER: Final = "x-forwarded-proto"

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class InvalidTrustedProxyError(ValueError):
    """A configured trusted proxy is not a usable IP address or CIDR block."""


def parse_trusted_proxy(value: str) -> IpNetwork:
    """Parse one configured entry into a network, or refuse it with an actionable message.

    A bare address is accepted and becomes a single-host network, so configuration can say
    ``10.0.0.7`` rather than ``10.0.0.7/32``. ``strict=False`` accepts a block written with
    host bits set instead of rejecting it on a technicality.
    """
    text = value.strip()
    if not text:
        raise InvalidTrustedProxyError(
            "TRUSTED_PROXIES contains an empty entry. Use a comma-separated list of IP "
            "addresses or CIDR blocks, for example: 10.0.0.0/8,192.168.1.5"
        )
    try:
        return ipaddress.ip_network(text, strict=False)
    except ValueError as exc:
        raise InvalidTrustedProxyError(
            f"TRUSTED_PROXIES entry {text!r} is not a valid IP address or CIDR block ({exc}). "
            "Expected something like 10.0.0.0/8, 192.168.1.5, or 2001:db8::/32."
        ) from exc


@lru_cache(maxsize=32)
def trusted_networks(configured: tuple[str, ...]) -> tuple[IpNetwork, ...]:
    """The parsed trusted-proxy networks.

    Cached on the configured tuple because this is consulted on every request while the
    configuration is fixed for the life of the process. Keyed by value, so a test that builds
    an application with different settings gets its own entry rather than a stale one.
    """
    return tuple(parse_trusted_proxy(entry) for entry in configured)


def parse_ip(value: str) -> IpAddress | None:
    """An IP address from one chain entry, or None if it is not one.

    Handles the forms that legitimately appear in ``X-Forwarded-For``: a bare IPv4 or IPv6
    address, IPv4 with a port, and the bracketed ``[2001:db8::1]:443`` form. A bare IPv6
    address is NOT split on ``:`` -- it is made of colons -- which is the bug that makes naive
    port-stripping mangle IPv6 into something unparseable.
    """
    text = value.strip()
    if not text:
        return None

    if text.startswith("["):
        closing = text.find("]")
        if closing == -1:
            return None
        text = text[1:closing]
    elif text.count(":") == 1:
        # Exactly one colon means IPv4 with a port; IPv6 always has two or more.
        text = text.split(":", 1)[0]

    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def is_trusted(address: IpAddress, networks: tuple[IpNetwork, ...]) -> bool:
    """Whether *address* sits inside any configured trusted network.

    An IPv4 address is never inside an IPv6 network and vice versa, and ``in`` raises on that
    mismatch rather than returning False, so the version is compared first.
    """
    return any(address.version == net.version and address in net for net in networks)


def forwarded_values(header_values: list[str]) -> list[str]:
    """Flatten one or more ``X-Forwarded-For`` headers into a single ordered chain.

    A proxy may append to the existing header or add a second one; both are legal and mean the
    same thing, so they are concatenated in the order received. Leftmost is the value furthest
    from us -- the least trustworthy end.
    """
    chain: list[str] = []
    for header in header_values:
        chain.extend(part for part in (piece.strip() for piece in header.split(",")) if part)
    return chain


def resolve_client_ip(
    peer: str | None,
    forwarded_for: list[str],
    networks: tuple[IpNetwork, ...],
) -> str:
    """The client address, per the algorithm in the module docstring.

    *peer* is the TCP peer address; *forwarded_for* is the raw header values, unparsed.
    """
    if peer is None:
        return UNKNOWN_ADDRESS

    peer_ip = parse_ip(peer)
    # Nothing may be read from a header unless a trusted proxy is configured AND the peer is
    # one of them. Both halves matter: the first makes the default safe, and the second is
    # what a direct connection cannot satisfy no matter what it sends.
    if not networks or peer_ip is None or not is_trusted(peer_ip, networks):
        return peer

    for entry in reversed(forwarded_values(forwarded_for)):
        candidate = parse_ip(entry)
        if candidate is None:
            # Malformed. Stop rather than skip: skipping past garbage would let an attacker
            # hide a hop behind it and push the walk further left than the truth allows.
            return peer
        if not is_trusted(candidate, networks):
            return str(candidate)

    # Every hop was our own infrastructure; no external client is identifiable.
    return peer


def resolve_scheme(
    scheme: str,
    peer: str | None,
    forwarded_proto: list[str],
    networks: tuple[IpNetwork, ...],
) -> str:
    """The scheme the ORIGINAL client used, which is what HSTS has to be decided on.

    A TLS-terminating proxy speaks plain HTTP to us, so ``scope["scheme"]`` reports ``http``
    for a request the browser made over HTTPS. ``X-Forwarded-Proto`` carries the truth, and is
    read under exactly the same trust rule as the address because it is exactly as forgeable.
    """
    if peer is None or not networks:
        return scheme

    peer_ip = parse_ip(peer)
    if peer_ip is None or not is_trusted(peer_ip, networks):
        return scheme

    for header in forwarded_proto:
        # A chain may carry several; the leftmost is what the client actually spoke.
        first = header.split(",")[0].strip().lower()
        if first in {"http", "https"}:
            return first
    return scheme


__all__ = [
    "FORWARDED_FOR_HEADER",
    "FORWARDED_PROTO_HEADER",
    "UNKNOWN_ADDRESS",
    "InvalidTrustedProxyError",
    "forwarded_values",
    "is_trusted",
    "parse_ip",
    "parse_trusted_proxy",
    "resolve_client_ip",
    "resolve_scheme",
    "trusted_networks",
]
