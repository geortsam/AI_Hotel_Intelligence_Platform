#!/bin/sh
# Writes the `resolver` directive that frontend/nginx.conf includes, from the DNS server this
# container was actually handed.
#
# ## Why this file exists at all
#
# nginx resolves a literal upstream name -- `proxy_pass http://api:8000;` -- exactly ONCE, when
# it loads its configuration, and then keeps that address for the life of that configuration.
# Recreate the api container and Docker's IPAM gives the replacement a different address, while
# this nginx goes on dialling the old one: 502 on every /api request, for as long as the process
# lives. Worse, its own /healthz keeps answering, so the container still reports healthy and
# nothing signals the breakage. The only recovery was restarting the frontend.
#
# Resolving through a VARIABLE instead makes nginx look the name up per request. But that needs a
# `resolver`, and `resolver` cannot contain variables -- nginx parses it at configuration load --
# so the address has to be on disk before nginx starts. Hence a startup script rather than a
# line in nginx.conf.
#
# ## Why not just hard-code 127.0.0.11
#
# That is Docker's embedded DNS on a user-defined network, and it is where this stack's lookups
# do go today. But it is a property of how the container is RUN, not of this image: a container
# on the host network, or under a different runtime, is handed something else entirely, and a
# hard-coded address would then point nginx at nothing. /etc/resolv.conf is the one source that
# is correct by construction -- it is what the container's own libc resolver already uses, so
# reading it cannot disagree with reality.
#
# ## How it runs
#
# Through the nginx image's own /docker-entrypoint.d/ convention: the image entrypoint executes
# every executable *.sh there, in sort order, before exec'ing nginx. It therefore also runs for
# `nginx -t`, which is what keeps configuration validation honest.
#
# If it does NOT run, /etc/nginx/resolver.conf is absent and nginx refuses to start, naming the
# missing file. That is the right failure: loud, at startup, rather than a silent fall back to
# resolving once and going stale again.
set -eu

target=/etc/nginx/resolver.conf

# One entry per `nameserver` line, IPv6 addresses bracketed as nginx requires. awk rather than
# grep piped into cut so a malformed or commented line cannot yield a half-parsed address, and
# so the address family is decided by the address itself.
servers="$(awk '
    $1 == "nameserver" && $2 != "" {
        if ($2 ~ /:/) printf "[%s] ", $2; else printf "%s ", $2
    }' /etc/resolv.conf)"

if [ -z "$servers" ]; then
    echo "$0: no nameserver in /etc/resolv.conf -- nginx would have no way to re-resolve its" >&2
    echo "$0: upstream, so refusing to start rather than starting with a stale address." >&2
    exit 1
fi

# `valid=10s` overrides the record's own TTL. Docker's embedded DNS answers with 600s, which
# would leave a replaced api container unreachable for ten minutes -- technically self-healing,
# operationally indistinguishable from broken.
#
# `ipv6=off` because this compose network is IPv4. Without it nginx asks for AAAA on every
# lookup as well and waits for an answer it could not use.
#
# `resolver_timeout` bounds a DNS failure: nginx's default is 30s, so an unreachable resolver
# would turn every request into a half-minute hang instead of a prompt 502.
{
    printf 'resolver %svalid=10s ipv6=off;\n' "$servers"
    printf 'resolver_timeout 5s;\n'
} > "$target"

echo "$0: $target -> $(tr '\n' ' ' < "$target")"
