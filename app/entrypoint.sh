#!/bin/sh
# Render the two templated configs, then hand off to nginx.
set -eu

: "${PUBLIC_ORIGIN:=http://localhost:8080}"
: "${LLM_MODEL_ID:=qwen3-8b}"
: "${LLM_MODEL_LABEL:=Local model}"

TPL=/etc/nginx/templates.glen
HTML=/usr/share/nginx/html

# Client-side config. The explicit variable allowlist is not optional: a bare
# `envsubst` would also eat any $-sign that happens to appear in a label.
envsubst '${LLM_MODEL_ID} ${LLM_MODEL_LABEL}' \
    < "$TPL/config.json.template" > "$HTML/config.json"

# Server-side nginx config. This template contains nginx's own $uri, $host and
# $http_range, so the allowlist here is what stops envsubst from blanking them
# — an empty allowlist would substitute every variable and produce a config
# that fails to start with an error pointing nowhere near the cause.
# It currently needs no substitutions at all; the empty list is deliberate and
# keeps the copy honest about that.
envsubst '' < "$TPL/nginx.conf.template" > /etc/nginx/conf.d/default.conf

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
echo "  geo-agent     : $(cat "$HTML/app/GEO_AGENT_VERSION" 2>/dev/null || echo unknown)"
echo "  If layers 404, check that PUBLIC_ORIGIN matches the URL in your browser."

exec "$@"
