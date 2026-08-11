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

# Protomaps publishes a dated build each day, and pinning one is still the right
# default — an unpinned basemap makes two boxes built a week apart subtly
# different. But be clear about the limit: build.protomaps.com retains only a
# few recent weeks, so a pin is reproducible for a while, NOT forever. When this
# date ages out the archive 404s and the only fix is to pick a newer one, which
# is why the URL is checked before the long extract starts rather than after.
#
# Override with PROTOMAPS_BUILD=YYYYMMDD, or point PROTOMAPS_URL at your own
# mirror if you need a genuinely durable pin.
#
# This one was current and verified on 2026-08-11 (a full US extract built from
# it). Expect it to 404 within weeks — that is the retention window, not a
# mistake, and the check below will tell you so rather than failing obscurely.
PROTOMAPS_BUILD="${PROTOMAPS_BUILD:-20260810}"
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
    # Check before committing to a 10-40 minute range-request crawl. An aged-out
    # pin otherwise surfaces as an opaque failure partway through the extract.
    if ! curl -fsI --max-time 60 "$PROTOMAPS_URL" >/dev/null 2>&1; then
        cat >&2 <<EOF

Cannot reach the Protomaps build:
  $PROTOMAPS_URL

If that is a 404, the pinned build has aged out — build.protomaps.com keeps
only the last few weeks of daily builds. Pick a recent date and retry:

    PROTOMAPS_BUILD=\$(date -u +%Y%m%d) make basemap

then update PROTOMAPS_BUILD in basemap/build.sh so the pin matches what you
actually built. Yesterday's date is a safer bet than today's if the current
day's build has not finished publishing.

EOF
        exit 1
    fi

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
# The Protomaps base themes are ~70 layers each. Hand-writing them here would
# be unmaintainable, so generate from the published package instead.
#
# Package name matters: `protomaps-themes-base` stops at 4.5.0. From v5 the
# package was renamed to `@protomaps/basemaps`, and the flavor argument changed
# from a string to a flavor object obtained via namedFlavor(). Asking npm for
# protomaps-themes-base@5.x installs nothing and fails the build.
THEMES_PKG="${THEMES_PKG:-@protomaps/basemaps@5.7.2}"

echo "==> generating styles (${THEMES_PKG})"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
(
    cd "$WORK"
    npm init -y >/dev/null 2>&1
    npm install --no-audit --no-fund --loglevel=error "$THEMES_PKG" >/dev/null
)

# --input-type=module: the heredoc below is ESM, and Node only guesses that for
# stdin on recent versions. Stating it keeps this working on any Node 22.
STYLES_OUT="$PWD/config/styles" \
WORKDIR="$WORK" \
node --input-type=module - <<'NODE'
import { writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(process.env.WORKDIR + '/');
const themes = require('@protomaps/basemaps');

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
        // Relative to options.paths.sprites in config/config.json, which is
        // already /data/sprites — so this is 'v4/light', NOT 'sprites/v4/light'.
        // Getting it wrong makes tileserver-gl look for
        // /data/sprites/sprites/v4/light@2x.png, and a sprite that fails to
        // load takes the whole render down: every tile request 500s.
        sprite: `v4/${flavor}`,
        sources: {
            [SOURCE]: { type: 'vector', url: SOURCE_URL, attribution: '© OpenStreetMap contributors' },
        },
        // namedFlavor(), not the bare string: v5 takes a flavor OBJECT. Passing
        // a string still returns layers, but silently a different (and smaller)
        // set — a basemap that renders and is quietly missing features.
        layers: themes.layers(SOURCE, themes.namedFlavor(flavor), { lang: 'en' }),
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
