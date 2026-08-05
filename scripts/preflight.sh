#!/usr/bin/env bash
# Check that the three large downloaded artifacts are present before starting.
#
# Each of these fails at a different, unhelpful moment if missing: no weights
# means llama.cpp crashloops with "failed to load model"; no basemap means
# tileserver-gl exits and the map is grey; no data bundle means the catalog is
# empty and the agent politely reports that it has no datasets. Checking here
# turns three confusing runtime symptoms into one clear message.
set -uo pipefail

cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
[ -f .env ] && { set -a; . ./.env; set +a; }

MODEL_FILE="${LLM_MODEL_FILE:-Qwen3-8B-Q4_K_M.gguf}"

missing=0

check() {
    local label="$1" path="$2" fix="$3"
    if [ -e "$path" ] && [ -n "$(ls -A "$path" 2>/dev/null || echo x)" ]; then
        printf '  ✓ %-24s %s\n' "$label" "$path"
    else
        printf '  ✗ %-24s missing: %s\n' "$label" "$path"
        printf '      fix: %s\n' "$fix"
        missing=1
    fi
}

echo "==> preflight"
check "LLM weights"   "llm/models/$MODEL_FILE"        "make model"
check "basemap tiles" "basemap/data/us.pmtiles"       "make basemap"
check "basemap glyphs" "basemap/data/fonts"           "make basemap"
check "basemap styles" "basemap/config/styles/glen-light.json" "make basemap"
check "census bundle" "data/bundle/stac/catalog.json" "make data"

if [ "$missing" -ne 0 ]; then
    cat >&2 <<'EOF'

Preflight failed. These artifacts are large and are not committed to git, so a
fresh clone always needs them built once:

    make bundle

Or, if you were given a prebuilt archive:

    make load

EOF
    exit 1
fi

echo "  all artifacts present"
