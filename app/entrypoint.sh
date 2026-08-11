#!/bin/sh
# Render the two templated configs, then hand off to nginx.
set -eu

: "${PUBLIC_ORIGIN:=http://localhost:8080}"
: "${LLM_MODEL_ID:=qwen3-8b}"
: "${LLM_MODEL_LABEL:=Local model}"

# llama.cpp needs no credential, but the OpenAI-compatible client always sends
# `Authorization: Bearer <api_key>`, so there is a header to put something in.
# Default "local" keeps a laptop unguarded and unchanged.
#
# Set it to a real secret when this box is reachable from anywhere else. It is
# the only thing standing in front of the LLM route, because that route CANNOT be
# protected by an authenticating reverse proxy: the app overwrites Authorization
# with its own Bearer value on every request, so a proxy doing basic auth sees a
# non-Basic header, 401s, and the browser re-prompts forever — signing in cannot
# fix it. Exempt /api/llm/ at the proxy and let this guard cover it instead. The
# token reaches the browser through config.json, so keep THAT behind the proxy's
# auth and the token stays unguessable.
: "${LLM_API_KEY:=local}"

TPL=/etc/nginx/templates.glen
HTML=/usr/share/nginx/html

# Client-side config. The explicit variable allowlist is not optional: a bare
# `envsubst` would also eat any $-sign that happens to appear in a label.
envsubst '${PUBLIC_ORIGIN} ${LLM_MODEL_ID} ${LLM_MODEL_LABEL} ${LLM_API_KEY}' \
    < "$TPL/config.json.template" > "$HTML/config.json"

# Guard for the LLM route, built here rather than in the template because it is
# conditional. With the default token there is no guard at all, which keeps a
# laptop working exactly as before — including `make verify` probing
# /api/llm/models with no credential.
#
# $http_authorization survives envsubst because only the names in the allowlist
# below are substituted; the guard's own text is inserted verbatim.
if [ "$LLM_API_KEY" = "local" ]; then
    LLM_AUTH_GUARD="# no LLM token configured (LLM_API_KEY unset) — route is open"
else
    LLM_AUTH_GUARD='if ($http_authorization != "Bearer '"$LLM_API_KEY"'") { return 401; }'
fi
export LLM_AUTH_GUARD

# The address of the embedded DNS that resolves sibling service names for
# nginx's request-time `resolver`. It is runtime-specific and must not be
# hardcoded: Docker answers on 127.0.0.11, but podman's aardvark-dns answers on
# the network gateway (10.89.0.1 or similar). Naming the wrong one fails in a
# particularly slow, misleading way — nginx starts happily and every static file
# serves, so the app loads and looks fine, while /data/, /mcp, /titiler and
# /basemap each hang for the full resolver timeout and then return nothing.
# scripts/verify.sh shows the shape of it exactly: the whole `app shell` section
# passes and every proxied route reports HTTP 000.
#
# Both runtimes write the right address into the container's own resolv.conf, so
# read it from there. IPv4 only: an IPv6 nameserver would need bracketing in the
# resolver directive.
#
# Read once, at start. Podman's DNS address can move when the network is
# recreated, so recreating the network means recreating this container too —
# `docker compose up -d --force-recreate webapp` — or the resolver goes stale and
# the same silent hang comes back.
: "${DNS_RESOLVER:=$(awk '/^nameserver/ && $2 ~ /^[0-9.]+$/ { print $2; exit }' /etc/resolv.conf 2>/dev/null)}"
: "${DNS_RESOLVER:=127.0.0.11}"
export DNS_RESOLVER

# Server-side nginx config. This template contains nginx's own $uri, $host and
# $http_range, so the allowlist here is what stops envsubst from blanking them
# — a bare `envsubst` would substitute every variable and produce a config that
# fails to start with an error pointing nowhere near the cause.
#
# BOTH names are required. Dropping either one leaves its placeholder literal in
# the output and nginx refuses to start with `unknown directive
# "${...}"` — loud, but if you ever "fix" that by deleting the placeholder
# instead of restoring it here, the LLM route silently loses its token check.
envsubst '${DNS_RESOLVER} ${LLM_AUTH_GUARD}' < "$TPL/nginx.conf.template" > /etc/nginx/conf.d/default.conf

# nginx defaults to one worker per CPU. On a many-core workstation that is
# hundreds of processes fighting over a container that only ever serves a
# handful of concurrent users, and it has OOM-killed small containers in
# related deployments. Two is plenty for a single-user box.
sed -i 's/^worker_processes.*/worker_processes 2;/' /etc/nginx/nginx.conf

# PUBLIC_ORIGIN is used three ways, which is why a mismatch is so confusing:
# config.json's mcp_server_url is built from it here, the seed job bakes it into
# STAC hrefs, and the MCP server rewrites those hrefs back to s3://. Surfacing it
# in the log makes a mismatch — the single most common cause of both "layers do
# not load" and "MCP server unreachable" — obvious.
echo "glen-in-a-box webapp"
echo "  PUBLIC_ORIGIN : $PUBLIC_ORIGIN"
echo "  DNS resolver  : $DNS_RESOLVER"
echo "  LLM model     : $LLM_MODEL_ID ($LLM_MODEL_LABEL)"
if [ "$LLM_API_KEY" = "local" ]; then
    echo "  LLM auth      : OPEN — anyone who can reach /api/llm/ can use the model"
else
    echo "  LLM auth      : bearer token required on /api/llm/"
fi
echo "  geo-agent     : $(cat "$HTML/app/GEO_AGENT_VERSION" 2>/dev/null || echo unknown)"
echo "  If layers 404, check that PUBLIC_ORIGIN matches the URL in your browser."

exec "$@"
