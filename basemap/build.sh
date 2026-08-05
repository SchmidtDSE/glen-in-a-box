#!/usr/bin/env bash
# Build the offline basemap: a US extract of the Protomaps daily build, the
# glyph and sprite assets that go with it, and three rendered styles.
#
# ONLINE STEP. Run once via `make basemap`. Everything it produces lands in
# basemap/data and basemap/config, both gitignored.
#
# ── Why an extract and not a download ────────────────────────────────────────
# The Protomaps daily build is a ~120 GB planet PMTiles archive. `pmtiles
# extract` reads it over HTTP range requests and writes only the tiles covering
# our bounding box, so a US extract costs a few GB of transfer instead of 120.
#
# ── Why three styles ─────────────────────────────────────────────────────────
# geo-agent's UI has three basemap buttons wired to three named sources. Rather
# than delete two buttons, patch-geoagent.mjs points them at light, dark and
# white variants of the same underlying extract — no extra data, three genuinely
# useful looks under bright choropleths.
set -euo pipefail

cd "$(dirname "$0")"

# Protomaps publishes a dated build each day. Pin one: "latest" is not
# reproducible, and a rebuild six months from now should produce the same box.
PROTOMAPS_BUILD="${PROTOMAPS_BUILD:-20260701}"
PROTOMAPS_URL="${PROTOMAPS_URL:-https://build.protomaps.com/${PROTOMAPS_BUILD}.pmtiles}"

# US including Alaska and Hawaii. Alaska crosses the antimeridian, so the west
# edge is -180 rather than the ~-168 the mainland would suggest.
BBOX="${BASEMAP_BBOX:--180,15.0,-64.0,72.0}"

# z12 gives clean county- and city-level context at roughly 1-2 GB. z13-14 look
# better at street level and cost several times more; below z10 the map turns
# to mush as soon as anyone zooms into a tract.
MAXZOOM="${BASEMAP_MAXZOOM:-12}"

PMTILES_VERSION="${PMTILES_VERSION:-1.22.1}"
ASSETS_REF="${ASSETS_REF:-main}"

mkdir -p data config/styles bin

# ── go-pmtiles ───────────────────────────────────────────────────────────────
if [ ! -x bin/pmtiles ]; then
    echo "==> fetching go-pmtiles ${PMTILES_VERSION}"
    case "$(uname -m)" in
        x86_64|amd64) ARCH=x86_64 ;;
        aarch64|arm64) ARCH=arm64 ;;
        *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
    esac
    case "$(uname -s)" in
        Linux) OS=Linux ;;
        Darwin) OS=Darwin ;;
        *) echo "unsupported OS: $(uname -s)" >&2; exit 1 ;;
    esac
    curl -fsSL -o /tmp/pmtiles.tar.gz \
        "https://github.com/protomaps/go-pmtiles/releases/download/v${PMTILES_VERSION}/go-pmtiles_${PMTILES_VERSION}_${OS}_${ARCH}.tar.gz"
    tar -xzf /tmp/pmtiles.tar.gz -C bin pmtiles
    rm -f /tmp/pmtiles.tar.gz
    chmod +x bin/pmtiles
fi

# ── Tile extract ─────────────────────────────────────────────────────────────
if [ ! -s data/us.pmtiles ]; then
    echo "==> extracting US tiles from ${PROTOMAPS_URL}"
    echo "    bbox=${BBOX} maxzoom=${MAXZOOM}"
    echo "    This is the long step — expect 10-40 minutes and a few GB."
    ./bin/pmtiles extract "${PROTOMAPS_URL}" data/us.pmtiles \
        --bbox="${BBOX}" \
        --maxzoom="${MAXZOOM}"
else
    echo "==> data/us.pmtiles exists, skipping extract (delete it to rebuild)"
fi

# ── Glyphs and sprites ───────────────────────────────────────────────────────
# MapLibre cannot render a single text label without glyph PBFs, and there is
# no fallback path — an unreachable glyph endpoint is a map that renders
# geometry and no place names. These come from Protomaps' asset repo, which
# ships the exact font stacks the themes reference.
if [ ! -d data/fonts ] || [ -z "$(ls -A data/fonts 2>/dev/null)" ]; then
    echo "==> fetching glyphs and sprites (protomaps/basemaps-assets@${ASSETS_REF})"
    rm -rf /tmp/basemaps-assets
    git clone --depth 1 --branch "${ASSETS_REF}" \
        https://github.com/protomaps/basemaps-assets.git /tmp/basemaps-assets
    cp -r /tmp/basemaps-assets/fonts data/fonts
    cp -r /tmp/basemaps-assets/sprites data/sprites
    rm -rf /tmp/basemaps-assets
else
    echo "==> data/fonts exists, skipping asset fetch"
fi

# ── Styles ───────────────────────────────────────────────────────────────────
# The Protomaps base themes are ~80 layers each. Hand-writing them here would
# be unmaintainable, so generate from the published package instead.
echo "==> generating styles"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
(
    cd "$WORK"
    npm init -y >/dev/null 2>&1
    npm install --no-audit --no-fund --loglevel=error protomaps-themes-base@5.0.0 >/dev/null
)

STYLES_OUT="$PWD/config/styles" \
WORKDIR="$WORK" \
node - <<'NODE'
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(process.env.WORKDIR + '/');
const themes = require('protomaps-themes-base');

// Source name is referenced by every generated layer, so it has to match the
// key in the style's `sources` block exactly.
const SOURCE = 'protomaps';

// tileserver-gl resolves this scheme against the `data` entry of the same name
// in config/config.json. If tiles come back empty, this string is the first
// thing to check — see basemap/README.md.
const SOURCE_URL = 'mbtiles://{us}';

for (const flavor of ['light', 'dark', 'white']) {
    const style = {
        version: 8,
        name: `glen-${flavor}`,
        // Served by tileserver-gl from data/fonts and data/sprites, reached by
        // the browser through the /basemap/ nginx route.
        glyphs: '{fontstack}/{range}.pbf',
        sprite: `sprites/v4/${flavor}`,
        sources: {
            [SOURCE]: { type: 'vector', url: SOURCE_URL, attribution: '© OpenStreetMap contributors' },
        },
        layers: themes.layers(SOURCE, flavor, { lang: 'en' }),
    };
    writeFileSync(`${process.env.STYLES_OUT}/glen-${flavor}.json`, JSON.stringify(style, null, 2));
    console.log(`    wrote glen-${flavor}.json (${style.layers.length} layers)`);
}
NODE

echo ""
echo "==> basemap ready"
du -sh data/us.pmtiles data/fonts data/sprites 2>/dev/null || true
echo ""
echo "Verify once the stack is up:"
echo "  make basemap-verify"
