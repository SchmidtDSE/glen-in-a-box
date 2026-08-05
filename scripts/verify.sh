#!/usr/bin/env bash
# End-to-end check of a running stack.
#
# Every probe goes through the public origin rather than straight at a
# container, because that is the path the browser takes — a service can be
# perfectly healthy and still be unreachable through a mistyped proxy rule,
# and that is the failure this is meant to catch.
#
#   scripts/verify.sh            run everything
#   scripts/verify.sh basemap    run only the basemap probes
set -uo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
[ -f .env ] && { set -a; . ./.env; set +a; }

ORIGIN="${PUBLIC_ORIGIN:-http://localhost:8080}"
ONLY="${1:-all}"

pass=0
fail=0

# probe <label> <url> [min-bytes] [expected-content-type-substring]
probe() {
    local label="$1" url="$2" minbytes="${3:-1}" ctype="${4:-}"
    local body code got_ctype size

    body=$(mktemp)
    code=$(curl -s -o "$body" -w '%{http_code}' --max-time 30 "$url" 2>/dev/null)
    got_ctype=$(curl -s -o /dev/null -w '%{content_type}' --max-time 30 "$url" 2>/dev/null)
    size=$(wc -c < "$body" | tr -d ' ')

    if [ "$code" != "200" ]; then
        printf '  ✗ %-38s HTTP %s\n' "$label" "$code"
        fail=$((fail + 1))
    elif [ "$size" -lt "$minbytes" ]; then
        # A 200 with a tiny body is the signature of a proxy that answered
        # instead of the service behind it.
        printf '  ✗ %-38s 200 but only %s bytes (expected ≥ %s)\n' "$label" "$size" "$minbytes"
        fail=$((fail + 1))
    elif [ -n "$ctype" ] && [[ "$got_ctype" != *"$ctype"* ]]; then
        printf '  ✗ %-38s wrong content-type: %s\n' "$label" "$got_ctype"
        fail=$((fail + 1))
    else
        printf '  ✓ %-38s %s bytes\n' "$label" "$size"
        pass=$((pass + 1))
    fi
    rm -f "$body"
}

echo "==> verifying $ORIGIN"

if [ "$ONLY" = "all" ]; then
    echo ""
    echo "  app shell"
    probe "index.html"            "$ORIGIN/"                        500
    probe "config.json"           "$ORIGIN/config.json"             50
    probe "layers-input.json"     "$ORIGIN/layers-input.json"       500
    probe "system-prompt.md"      "$ORIGIN/system-prompt.md"        200
    probe "geo-agent main.js"     "$ORIGIN/app/main.js"             1000
    probe "vendored maplibre"     "$ORIGIN/vendor/maplibre-gl/maplibre-gl.js" 100000
    probe "vendored MCP SDK"      "$ORIGIN/vendor/mcp/client-index.js"        1000
    # Easy to forget and invisible until someone asks for their first chart.
    probe "vendored d3 (lazy)"    "$ORIGIN/vendor/d3/d3.min.js"      100000
    probe "vendored Plot (lazy)"  "$ORIGIN/vendor/plot/plot.umd.min.js" 50000

    echo ""
    echo "  data"
    probe "STAC catalog"          "$ORIGIN/data/geo-data/stac/catalog.json" 200
    probe "states collection"     "$ORIGIN/data/geo-data/stac/us-census-states/collection.json" 200
    probe "counties collection"   "$ORIGIN/data/geo-data/stac/us-census-counties/collection.json" 200
    probe "tracts collection"     "$ORIGIN/data/geo-data/stac/us-census-tracts/collection.json" 200
    probe "states pmtiles header" "$ORIGIN/data/geo-data/pmtiles/us-census-states.pmtiles" 100

    # The catalog is the one place a PUBLIC_ORIGIN mismatch shows up as data
    # rather than as a failed request, so check it directly.
    echo ""
    echo "  catalog origin"
    catalog=$(curl -s --max-time 20 "$ORIGIN/data/geo-data/stac/catalog.json")
    if grep -q '${PUBLIC_ORIGIN}' <<<"$catalog"; then
        printf '  ✗ %-38s unsubstituted placeholder — re-run: make seed\n' "hrefs rendered"
        fail=$((fail + 1))
    elif grep -q "$ORIGIN" <<<"$catalog"; then
        printf '  ✓ %-38s hrefs match %s\n' "hrefs rendered" "$ORIGIN"
        pass=$((pass + 1))
    else
        printf '  ✗ %-38s hrefs do NOT contain %s\n' "hrefs rendered" "$ORIGIN"
        printf '      The bundle was seeded with a different PUBLIC_ORIGIN.\n'
        printf '      Fix PUBLIC_ORIGIN in .env and re-run: make seed\n'
        fail=$((fail + 1))
    fi

    echo ""
    echo "  services"
    probe "MCP health"            "$ORIGIN/mcp" 1
    probe "TiTiler"               "$ORIGIN/titiler/healthz" 1

    echo ""
    echo "  LLM"
    models=$(curl -s --max-time 30 "$ORIGIN/api/llm/models")
    if grep -q '"id"' <<<"$models"; then
        printf '  ✓ %-38s %s\n' "model loaded" "$(grep -o '"id":"[^"]*"' <<<"$models" | head -1)"
        pass=$((pass + 1))
    else
        printf '  ✗ %-38s no model listed\n' "model loaded"
        printf '      Weights can take minutes to load on CPU. Check: make logs\n'
        fail=$((fail + 1))
    fi
fi

if [ "$ONLY" = "all" ] || [ "$ONLY" = "basemap" ]; then
    echo ""
    echo "  basemap"
    # A rendered tile under ~1 KB is almost certainly a blank PNG, which means
    # the style generated but resolved no data — see basemap/README.md.
    probe "raster tile (light)"  "$ORIGIN/basemap/styles/glen-light/4/4/6.png"  1000 "image/png"
    probe "raster tile (dark)"   "$ORIGIN/basemap/styles/glen-dark/4/4/6.png"   1000 "image/png"
    probe "glyph range"          "$ORIGIN/basemap/fonts/Noto%20Sans%20Regular/0-255.pbf" 1000
fi

echo ""
if [ "$fail" -eq 0 ]; then
    echo "==> all $pass checks passed"
    echo ""
    echo "Server side is healthy. To confirm the BROWSER is also offline-clean,"
    echo "disconnect this machine from the network, hard-reload the page, and"
    echo "watch the DevTools network tab for any request not going to $ORIGIN."
    exit 0
fi

echo "==> $fail check(s) failed, $pass passed"
echo "    Logs:  make logs"
echo "    Docs:  docs/troubleshooting.md"
exit 1
