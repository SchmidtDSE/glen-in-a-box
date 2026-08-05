#!/bin/sh
# Bundle @modelcontextprotocol/sdk into self-contained ES modules.
#
# Why this is not just two more rows in manifest.tsv:
#
# The upstream app resolves the SDK through an import map pointing at esm.sh.
# Those modules are not standalone files — they carry bare and absolute
# imports that esm.sh rewrites into further esm.sh URLs, transitively. You
# cannot snapshot them with curl; you would have to spider a dependency graph
# and rewrite every specifier, which is exactly what a bundler does properly.
#
# So we install the real npm package and bundle it locally.
#
# --splitting is load-bearing. The two entry points (Client and the
# StreamableHTTP transport) share SDK internals, and a transport constructed
# from a *different* module instance than the client will not interoperate:
# instanceof checks and shared module-level state both break, producing a
# connection that handshakes and then silently does nothing. Splitting emits
# one shared chunk so both entries reference the same instance.
#
# Usage: bundle-mcp-sdk.sh <sdk-version> <outdir>
set -eu

VERSION="${1:?usage: bundle-mcp-sdk.sh <version> <outdir>}"
OUTDIR="${2:?usage: bundle-mcp-sdk.sh <version> <outdir>}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cd "$WORK"
npm init -y >/dev/null 2>&1

echo "  installing @modelcontextprotocol/sdk@${VERSION} + esbuild"
npm install --no-audit --no-fund --loglevel=error \
    "@modelcontextprotocol/sdk@${VERSION}" \
    esbuild@0.25.0 >/dev/null

# Re-export stubs, one per import-map specifier the app uses.
mkdir -p src
echo "export * from '@modelcontextprotocol/sdk/client/index.js';" > src/client-index.js
echo "export * from '@modelcontextprotocol/sdk/client/streamableHttp.js';" > src/client-streamableHttp.js

mkdir -p "$OUTDIR"
echo "  bundling"
./node_modules/.bin/esbuild src/client-index.js src/client-streamableHttp.js \
    --bundle \
    --splitting \
    --format=esm \
    --platform=browser \
    --target=es2022 \
    --minify \
    --outdir="$OUTDIR" \
    --log-level=warning

# An empty or missing output here would surface much later as a blank chat
# panel with a bare "failed to resolve module" in the console, so assert now.
for f in client-index.js client-streamableHttp.js; do
    [ -s "$OUTDIR/$f" ] || { echo "bundle-mcp-sdk.sh: $f missing or empty" >&2; exit 1; }
done

echo "  bundled MCP SDK ${VERSION} → $OUTDIR"
