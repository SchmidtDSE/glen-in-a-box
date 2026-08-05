#!/usr/bin/env node
/**
 * Rewrite the hardcoded external URLs in a geo-agent snapshot to local routes.
 *
 * ── Why patching is unavoidable ──────────────────────────────────────────────
 * geo-agent is configurable in most places, but a handful of external URLs are
 * baked into module scope with no config surface: the MapLibre glyph endpoint,
 * two of the three built-in basemaps, the lazy-loaded chart and draw libraries,
 * and the upload bucket. Offline, each is a hang or a silent no-op. Since an
 * offline build has to vendor geo-agent's source anyway (it normally loads from
 * jsDelivr), rewriting those constants costs nothing extra.
 *
 * ── Why every replacement asserts ────────────────────────────────────────────
 * The failure mode this guards against is a version bump that moves or renames
 * one of these constants. A best-effort patcher would skip it and produce an
 * image that looks fine, boots fine, and then reaches for the internet the
 * first time a user draws a polygon. So a missing pattern is a hard build
 * failure, and a pattern that matches more than once is too — an unexpected
 * second occurrence means the file changed shape and the patch may be landing
 * somewhere it was never meant to.
 *
 * Fixed-string matching throughout: several patterns contain regex
 * metacharacters ({z}, ${ml}, ?) and sed-quoting them correctly is a losing game.
 *
 * Usage: node patch-geoagent.mjs <app-dir>
 */

import { readFileSync, writeFileSync, existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const appDir = process.argv[2];
if (!appDir) {
    console.error('usage: patch-geoagent.mjs <app-dir>');
    process.exit(1);
}

/** Local raster styles served by the tileserver-gl container (see basemap/). */
const STYLE = (id) => `/basemap/styles/${id}/{z}/{x}/{y}.png`;
const OSM_ATTRIB = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors — bundled offline extract';

const patches = [
    // ── Glyphs ───────────────────────────────────────────────────────────────
    // MapLibre fetches these lazily, only when a style contains a symbol
    // layer. Overlay layers here are fill/line/circle, so an unpatched build
    // appears to work — until someone adds a labelled layer and the map hangs
    // on a DNS lookup that will never resolve.
    {
        file: 'map-manager.js',
        note: 'glyph endpoint → local tileserver',
        from: "glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',",
        to: "glyphs: '/basemap/fonts/{fontstack}/{range}.pbf',",
    },

    // ── Basemaps ─────────────────────────────────────────────────────────────
    // All three built-ins are remote. We could point the first one at a local
    // tileserver via the `custom_basemap` config key instead of patching, but
    // that path also blanks the attribution string — and the bundled extract is
    // OpenStreetMap data under ODbL, which requires attribution. Patching all
    // three keeps the credit intact and gives the other two buttons somewhere
    // real to go.
    //
    // Axis order matters: the Esri endpoints are {z}/{y}/{x}; tileserver-gl is
    // {z}/{x}/{y}. Copying the template across without swapping produces a map
    // that renders tiles transposed about the diagonal, which reads as
    // "the basemap is subtly wrong" rather than as an outright failure.
    {
        file: 'map-manager.js',
        note: 'basemap 1 (NatGeo → local light)',
        from: "tiles: ['https://services.arcgisonline.com/ArcGIS/rest/services/NatGeo_World_Map/MapServer/tile/{z}/{y}/{x}'],",
        to: `tiles: ['${STYLE('glen-light')}'],`,
    },
    {
        file: 'map-manager.js',
        note: 'basemap 1 attribution',
        from: "attribution: 'Tiles &copy; Esri &mdash; National Geographic, Esri, DeLorme, NAVTEQ, UNEP-WCMC, USGS, NASA, ESA, METI, NRCAN, GEBCO, NOAA'",
        to: `attribution: '${OSM_ATTRIB}'`,
    },
    {
        file: 'map-manager.js',
        // No offline imagery is bundled — a satellite basemap would mean
        // shipping tens of GB. The button becomes a dark cartographic style,
        // which is genuinely useful over bright data layers.
        note: 'basemap 2 (Satellite → local dark; no offline imagery)',
        from: "tiles: ['https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],",
        to: `tiles: ['${STYLE('glen-dark')}'],`,
    },
    {
        file: 'map-manager.js',
        note: 'basemap 2 attribution',
        from: "attribution: 'Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community'",
        to: `attribution: '${OSM_ATTRIB}'`,
    },
    {
        file: 'map-manager.js',
        note: 'basemap 3 (Plain → local minimal)',
        from: "tiles: ['https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png'],",
        to: `tiles: ['${STYLE('glen-white')}'],`,
    },
    {
        file: 'map-manager.js',
        note: 'basemap 3 attribution',
        from: "attribution: '&copy; <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> contributors &copy; <a href=\"https://carto.com/\">CARTO</a>'",
        to: `attribution: '${OSM_ATTRIB}'`,
    },
    {
        file: 'map-manager.js',
        note: 'basemap button labels',
        from: `        const basemapDefs = [
            { key: 'natgeo',    label: this._customBasemapLabel || 'NatGeo' },
            { key: 'satellite', label: 'Satellite' },
            { key: 'plain',     label: 'Plain' },
        ];`,
        to: `        const basemapDefs = [
            { key: 'natgeo',    label: this._customBasemapLabel || 'Map' },
            { key: 'satellite', label: 'Dark' },
            { key: 'plain',     label: 'Minimal' },
        ];`,
    },

    // ── Lazy runtime loads ───────────────────────────────────────────────────
    // These fire minutes after page load, the first time a user asks for a
    // chart or opens the draw tool. The SRI constants must be blanked in the
    // same pass: a local URL carrying an upstream integrity hash fails the
    // check and refuses to execute, so patching the URL alone would trade a
    // network error for a subresource-integrity error.
    {
        file: 'chart-renderer.js',
        note: 'd3 → vendored',
        from: "const D3_JS = 'https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js';",
        to: "const D3_JS = '/vendor/d3/d3.min.js';",
    },
    {
        file: 'chart-renderer.js',
        note: 'd3 SRI cleared (verified at fetch time instead)',
        from: "const D3_JS_SRI = 'sha384-CjloA8y00+1SDAUkjs099PVfnY2KmDC2BZnws9kh8D/lX1s46w6EPhpXdqMfjK6i';",
        to: "const D3_JS_SRI = '';",
    },
    {
        file: 'chart-renderer.js',
        note: 'Observable Plot → vendored',
        from: "const PLOT_JS = 'https://cdn.jsdelivr.net/npm/@observablehq/plot@0.6.16/dist/plot.umd.min.js';",
        to: "const PLOT_JS = '/vendor/plot/plot.umd.min.js';",
    },
    {
        file: 'chart-renderer.js',
        note: 'Plot SRI cleared',
        from: "const PLOT_JS_SRI = 'sha384-XzQ+KW4LBWHv6FLjiPA1vjDz//oGlY8We4XROgpir5jHgg2+Qvo/6fT5TjbnqyYi';",
        to: "const PLOT_JS_SRI = '';",
    },
    {
        file: 'map-draw.js',
        note: 'mapbox-gl-draw js → vendored',
        from: "const DRAW_JS  = 'https://unpkg.com/@mapbox/mapbox-gl-draw@1.4.3/dist/mapbox-gl-draw.js';",
        to: "const DRAW_JS  = '/vendor/mapbox-gl-draw/mapbox-gl-draw.js';",
    },
    {
        file: 'map-draw.js',
        note: 'mapbox-gl-draw js SRI cleared',
        from: "const DRAW_JS_SRI = 'sha384-9tKNac0N0Yq08XtOQRfX8ivYwyzoSOriQtGhBKAxi2ezpT1C859IZ9bO1QNrKmMX';",
        to: "const DRAW_JS_SRI = '';",
    },
    {
        file: 'map-draw.js',
        note: 'mapbox-gl-draw css → vendored',
        from: "const DRAW_CSS = 'https://unpkg.com/@mapbox/mapbox-gl-draw@1.4.3/dist/mapbox-gl-draw.css';",
        to: "const DRAW_CSS = '/vendor/mapbox-gl-draw/mapbox-gl-draw.css';",
    },
    {
        file: 'map-draw.js',
        note: 'mapbox-gl-draw css SRI cleared',
        from: "const DRAW_CSS_SRI = 'sha384-4Y8+jUOlZUhQTrhjMAldWS0wpm/HjKvwEzKSXzLoaf3HJozxCpjiRXmXq71XbLT6';",
        to: "const DRAW_CSS_SRI = '';",
    },

    // ── Upload target ────────────────────────────────────────────────────────
    // upload_enabled is false in layers-input.json, so this is belt-and-braces:
    // if someone turns the feature on later, it should reach for the local
    // bucket rather than a Nautilus endpoint that is not there.
    {
        file: 'upload-manager.js',
        note: 'upload bucket → local rustfs',
        from: "bucketUrl: 'https://s3-west.nrp-nautilus.io/public-output',",
        to: "bucketUrl: '/data/uploads',",
    },

    // ── Clickable s3:// links in displayed SQL ───────────────────────────────
    // Cosmetic but visible: the chat pane rewrites s3:// paths inside SQL it
    // shows the user into clickable https links. Left alone, every such link
    // points at Nautilus and dead-ends. /data/ is the nginx route to the local
    // bucket, so the same click downloads the actual local object.
    {
        file: 'chat-ui.js',
        note: 's3:// display links → local /data/ route',
        from: '(_m, bucket, path) => `https://s3-west.nrp-nautilus.io/${bucket}${path || \'\'}`',
        to: '(_m, bucket, path) => `/data/${bucket}${path || \'\'}`',
    },

    // ── Exported standalone HTML ─────────────────────────────────────────────
    // The "export map" feature writes a self-contained HTML file whose <head>
    // pulls MapLibre and PMTiles from unpkg. Offline that export opens to a
    // blank page. Pointing it at /vendor/ makes the export work when served
    // from this same box; it will NOT work if the file is emailed to someone
    // else, which is the honest tradeoff and is called out in docs/offline.md.
    {
        file: 'chat-ui.js',
        note: 'exported-map <head> → vendored',
        from: '`<link href="https://unpkg.com/maplibre-gl@${ml}/dist/maplibre-gl.css" rel="stylesheet" crossorigin="anonymous">\n<script src="https://unpkg.com/maplibre-gl@${ml}/dist/maplibre-gl.js" crossorigin="anonymous"></script>\n<script src="https://unpkg.com/pmtiles@${pm}/dist/pmtiles.js" crossorigin="anonymous"></script>`',
        to: '`<link href="/vendor/maplibre-gl/maplibre-gl.css" rel="stylesheet">\n<script src="/vendor/maplibre-gl/maplibre-gl.js"></script>\n<script src="/vendor/pmtiles/pmtiles.js"></script>`',
    },
];

let failed = 0;
const dirty = new Map();

for (const p of patches) {
    const path = join(appDir, p.file);
    if (!existsSync(path)) {
        console.error(`  MISSING FILE  ${p.file}`);
        failed++;
        continue;
    }
    const src = dirty.get(path) ?? readFileSync(path, 'utf8');

    const occurrences = src.split(p.from).length - 1;
    if (occurrences === 0) {
        console.error(`  NOT FOUND     ${p.file}: ${p.note}`);
        console.error(`                looked for: ${JSON.stringify(p.from.slice(0, 90))}`);
        failed++;
        continue;
    }
    if (occurrences > 1) {
        console.error(`  AMBIGUOUS     ${p.file}: ${p.note} (${occurrences} occurrences)`);
        failed++;
        continue;
    }

    dirty.set(path, src.split(p.from).join(p.to));
    console.log(`  patched       ${p.file}: ${p.note}`);
}

if (failed > 0) {
    console.error(`
patch-geoagent.mjs: ${failed} patch(es) did not apply cleanly.

This almost always means GEO_AGENT_REF was bumped and one of these constants
moved, was renamed, or gained a config surface. Nothing has been written.

Do not skip the failing patch — compare against the new upstream source, update
the pattern here, and rebuild. An unpatched constant is an app that reaches for
the internet at some unpredictable later moment, which is much worse than a
failed build.`);
    process.exit(1);
}

for (const [path, content] of dirty) writeFileSync(path, content);

// ── Advisory sweep over the whole snapshot ───────────────────────────────────
// Catches any external URL the patch table does not know about — typically one
// introduced by a new upstream feature between GEO_AGENT_REF bumps.
//
// Advisory, not fatal. geo-agent legitimately contains external URLs that never
// execute offline: doc links in comments, and default config values that
// layers-input.json / config.json override. The allowlist below records the
// ones reviewed at v3.25.0 and *why* each is inert, so the next person bumping
// the ref sees only genuinely new hits instead of a wall of known noise.
const KNOWN_INERT = [
    ['w3.org', 'SVG/XML namespace, never fetched'],
    ['openstreetmap.org/copyright', 'attribution link we added'],
    ['titiler.nrp-nautilus.io', 'default overridden by titiler_url in layers-input.json'],
    ['api.maptiler.com', 'terrain, only reached when a maptiler key is configured — we set none'],
    ['carbon-api.nrp-nautilus.io', 'gated on links.carbon, which is false'],
    ['openrouter.ai', 'default endpoint in the settings UI, overridden by config.json'],
    ['llm-proxy.nrp-nautilus.io', 'transcription default; transcription is disabled'],
    ['duckdb-mcp.nrp-nautilus.io', 'default overridden by mcp_url'],
    ['s3-west.nrp-nautilus.io', 'remaining hits are comments and doc strings'],
    ['boettiger-lab.github.io', 'documentation link'],
    ['github.com/boettiger-lab', 'documentation link'],
    // The geocoder module is dead code here: layers-input.json omits the
    // `geocoder` block entirely, so map-geocoder.js is never dynamically
    // imported and geocoder.js never runs. Both of its backends are remote
    // services with no offline equivalent, so there is nothing to patch to —
    // the feature is off rather than redirected.
    ['nominatim.openstreetmap.org', 'geocoder backend; geocoder disabled in config'],
    ['photon.komoot.io', 'geocoder backend; geocoder disabled in config'],
    ['maplibre-gl-geocoder', 'geocoder widget, lazily imported only when geocoder is enabled'],
];

const jsFiles = readdirSync(appDir).filter((f) => f.endsWith('.js'));
const unknown = [];
for (const file of jsFiles) {
    const text = readFileSync(join(appDir, file), 'utf8');
    for (const m of text.matchAll(/https?:\/\/[^\s'"`)]+/g)) {
        if (KNOWN_INERT.some(([needle]) => m[0].includes(needle))) continue;
        unknown.push(`${file}: ${m[0]}`);
    }
}
if (unknown.length) {
    console.log('\n  ⚠ UNREVIEWED external URLs in the snapshot:');
    for (const u of [...new Set(unknown)]) console.log(`    ${u}`);
    console.log(`
  These appeared since the allowlist was last reviewed. Check whether each one
  can actually be reached at runtime. If it can, add a patch above; if it
  cannot, add it to KNOWN_INERT with the reason.`);
}

console.log(`\npatch-geoagent.mjs: ${patches.length} patch(es) applied to ${appDir}`);
