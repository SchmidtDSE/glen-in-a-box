# Offline basemap

Without this directory the map is a blank grey canvas with your data floating
on it. MapLibre's three built-in basemaps are all remote (`services.arcgisonline.com`
twice, `basemaps.cartocdn.com` once) and its glyph endpoint is hardcoded to
`demotiles.maplibre.org`, so there is nothing to fall back to.

## What gets built

`make basemap` runs [`build.sh`](build.sh), which produces:

| Path | What it is | Rough size |
|---|---|---|
| `data/us.pmtiles` | US extract of the Protomaps daily planet build, z0–12 | 1–2 GB |
| `data/fonts/` | Glyph PBFs — MapLibre renders no text without these | ~150 MB |
| `data/sprites/` | Icon sheets referenced by the themes | ~2 MB |
| `config/styles/glen-{light,dark,white}.json` | Generated from `protomaps-themes-base` | ~200 KB each |

`tileserver-gl` then renders raster tiles from the vector extract on demand and
serves the glyphs, both behind the `/basemap/` nginx route.

## Why raster, when the data is vector

geo-agent's basemap slots take a raster `{z}/{x}/{y}` template — the map style
is constructed with three raster sources and a button that swaps between them.
Serving vector tiles directly would mean restructuring that, so tileserver-gl
renders to PNG instead. The cost is CPU per tile; nginx caches the output for a
day, and after the first pan over an area it is essentially free.

## Verifying it works

The wiring most likely to need adjustment on first run is how the generated
styles reference the tile data. In `build.sh` that is one constant:

```js
const SOURCE_URL = 'mbtiles://{us}';
```

`{us}` resolves against the `data.us` entry in [`config/config.json`](config/config.json).
tileserver-gl uses the `mbtiles://` scheme as its generic local-data reference
regardless of the backing format, but if your tileserver-gl version wants
`pmtiles://{us}` instead, that is the single line to change — then re-run
`make basemap` (the extract is cached, so only the styles regenerate).

After `make up`:

```bash
make basemap-verify
```

which fetches a tile and a glyph range and tells you which one failed. A 200
with a nonzero body from both means the basemap is wired correctly.

If tiles come back as valid PNGs that are entirely one colour, the style
generated fine but found no data — that is the `SOURCE_URL` mismatch above,
not a problem with the extract.

## Tuning size

The extract dominates the bundle. Both knobs are environment variables:

```bash
BASEMAP_MAXZOOM=10 make basemap    # ~300 MB, fine for state/county work
BASEMAP_MAXZOOM=14 make basemap    # ~6 GB, street-level detail
BASEMAP_BBOX=-125,32,-114,42 make basemap   # California only
```

z12 is the default because census tracts are small: at z10 a tract boundary has
no recognisable context behind it.

## Attribution

The extract is OpenStreetMap data under [ODbL](https://www.openstreetmap.org/copyright).
`patch-geoagent.mjs` sets the attribution string on all three basemap sources so
the credit appears in the map corner. Keep it — it is a licence condition, not a
courtesy.
