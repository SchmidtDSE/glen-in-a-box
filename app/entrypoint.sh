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
envsubst '${LLM_MODEL_ID} ${LLM_MODEL_LABEL} ${LLM_API_KEY}' \
    < "$TPL/config.json.template" > "$HTML/config.json"

# Guard for the LLM route, built here rather than in the template because it is
# conditional. With the default token there is no guard at all, which keeps a
# laptop working exactly as before — including `make verify` probing
# /api/llm/models with no credential.
#
# $http_authorization survives envsubst because only ${LLM_AUTH_GUARD} is in the
# allowlist below; the guard's own text is inserted verbatim.
if [ "$LLM_API_KEY" = "local" ]; then
    LLM_AUTH_GUARD="# no LLM token configured (LLM_API_KEY unset) — route is open"
else
    LLM_AUTH_GUARD='if ($http_authorization != "Bearer '"$LLM_API_KEY"'") { return 401; }'
fi
export LLM_AUTH_GUARD

# Server-side nginx config. This template contains nginx's own $uri, $host and
# $http_range, so the allowlist here is what stops envsubst from blanking them
# — a bare `envsubst` would substitute every variable and produce a config that
# fails to start with an error pointing nowhere near the cause.
envsubst '${LLM_AUTH_GUARD}' < "$TPL/nginx.conf.template" > /etc/nginx/conf.d/default.conf

# nginx defaults to one worker per CPU. On a many-core workstation that is
# hundreds of processes fighting over a container that only ever serves a
# handful of concurrent users, and it has OOM-killed small containers in
# related deployments. Two is plenty for a single-user box.
sed -i 's/^worker_processes.*/worker_processes 2;/' /etc/nginx/nginx.conf

# PUBLIC_ORIGIN is not substituted into anything here — it is consumed by the
# seed job (which bakes it into STAC hrefs) and by the MCP server (which
# rewrites those hrefs back to s3://). Surfacing it in the log makes a
# mismatch, the single most common cause of "layers do not load", obvious.
echo "glen-in-a-box webapp"
echo "  PUBLIC_ORIGIN : $PUBLIC_ORIGIN"
echo "  LLM model     : $LLM_MODEL_ID ($LLM_MODEL_LABEL)"
if [ "$LLM_API_KEY" = "local" ]; then
    echo "  LLM auth      : OPEN — anyone who can reach /api/llm/ can use the model"
else
    echo "  LLM auth      : bearer token required on /api/llm/"
fi
echo "  geo-agent     : $(cat "$HTML/app/GEO_AGENT_VERSION" 2>/dev/null || echo unknown)"
echo "  If layers 404, check that PUBLIC_ORIGIN matches the URL in your browser."

exec "$@"
