# The data bundle

## What gets built

Two phases, and the split is the point.

**`make data` (online)** reads the upstream STAC and writes only what has to be
transformed — about 80 KB:

```
data/bundle/
├── stac/catalog.json, stac/<id>/collection.json   hrefs re-pointed at this box
├── stac/MIRROR-PROVENANCE.json                    what came from where
└── HYDRATE-MANIFEST.tsv                           dest<TAB>size<TAB>src, 71 objects
```

**`make seed` (online once)** executes that manifest, streaming each object
straight from the source bucket into the box's own rustfs — 15.6 GB, stored once:

```
s3://geo-data/
├── parquet/us-census-{blockgroups,tracts,counties,states}.parquet   1.2 GB
├── pmtiles/us-census-{...}.pmtiles                                  1.8 GB
├── hex/us-census-{...}/h0=*/data_0.parquet                          12  GB
└── stac/...
```

After that the box needs no network. The objects are in its object store, and
`make up-airgap` proves it — every backend loses its route out and the hex flow
still runs.

**Why not stage it on disk first.** The parquet, pmtiles and hex objects are
byte-identical copies; only the STAC needs rewriting. Downloading 15.6 GB to
`data/bundle/` and then uploading the same 15.6 GB into rustfs stored it twice
and moved it twice for no gain. Hydration skips any object already present with
the right size, which is what makes `make seed` cheap to re-run — and what makes
an air-gapped restore work without touching the network at all.

It also prunes: an object under a manifest-owned prefix (`parquet/`, `pmtiles/`,
`hex/`, `hex-fractions/`, `cog/`) that the manifest no longer lists is stale and
gets removed. Without that, dropping a layer leaves its objects in the bucket
forever, invisible and costing disk — which is exactly what happened when the
tract hex was removed and 6.7 GB stayed behind.

One wrinkle worth knowing: rustfs stages large uploads under `.rustfs.sys/tmp`
inside its volume and does not clear that promptly — after a full hydration it
held 6.6 GB. Restarting the rustfs container releases it.

Set `STAC_ONLY=0` to materialise everything on disk instead. You want that only
if you specifically need `data/bundle/` to be the portable artifact; the normal
air-gap path carries the object store, see below.

**Carrying it to a disconnected machine.** `make airgap` saves three archives:
images, the payload (STAC, model weights, basemap), and `objects.tar` — a dump of
the rustfs volume. That third one is the data. `make load` restores the volume,
and `make seed` on the target then finds all 71 objects already in place, fetches
nothing, and only re-renders the STAC for the local `PUBLIC_ORIGIN`.

A payload without `objects.tar` produces a box that starts, serves a catalog, and
has nothing behind it — so `airgap-save.sh` refuses to run before `make seed` and
warns if the archive comes out implausibly small.

## The raster layer

`us-nlcd-2024` is a COG, not a table: Annual NLCD Land Cover 2024, 30 m, CONUS,
1.6 GB, 203373 x 95653 px with nine overview levels. It is what TiTiler is in this
stack for — before it, `/titiler/` proxied a service with nothing to serve.

Its STAC asset href is an **`s3://` path, not `${PUBLIC_ORIGIN}`**, and that is
deliberate. The browser hands the href to TiTiler as `?url=`, and TiTiler resolves
it server-side with its own S3 configuration. A public-origin URL would send
TiTiler back out through nginx, which behind an authenticating proxy it cannot get
through — every tile would 401 while the rest of the app worked fine.

The palette is not configured here. The asset carries
`raster:bands[0].classification:classes` — 17 classes with a value, name,
description and `color_hint` each — and geo-agent builds the colormap and labels
the legend from that. So `layers-input.json` only says `legend_type:
"categorical"`, and the map shows real NLCD colours with real class names.

Land cover has no attributes to join on, so it pairs with the census layers
visually rather than in SQL: what surrounds the lowest-income block groups.

## The H3 hex assets, and why they are here

Every collection has three assets: `parquet` (one row per feature — counts,
rankings, aggregates), `pmtiles` (what the map renders), and `hex` (the same
attributes indexed to H3 cells). The land-cover collection adds a fourth,
`hex_fractions`.

**The hex indexes exist for cross-tessellation joins, and nothing else.** A raster
and census polygons divide space differently, so "what land cover is in this
county" is normally a spatial operation — expensive, and beyond what a small model
writes correctly. Index both to the same H3 cells and it collapses to a join on
`h10`:

```sql
WITH cells AS (
  SELECT DISTINCT h10
  FROM read_parquet('s3://geo-data/hex/us-census-blockgroups/h0=*/data_0.parquet')
  WHERE substr(GEOID, 1, 5) = '06019'
)
SELECT lc.nlcd, round(100.0 * sum(lc.frac) / (SELECT count(*) FROM cells), 1) AS pct
FROM cells c
JOIN read_parquet('s3://geo-data/hex-fractions/us-nlcd-2024/h0=*/data_0.parquet') lc
  ON lc.h10 = c.h10
GROUP BY lc.nlcd ORDER BY pct DESC
```

Which returns, for Fresno County: crops 33.5%, shrub 19.6%, grassland 19.1%,
evergreen forest 14.5% — Central Valley agriculture rising into the Sierra
foothills. Manhattan comes back developed-high 39.5% with a third open water.

The `DISTINCT h10` is load-bearing: several block groups can touch one cell, and
joining without deduplicating counts that cell once per block group and inflates
every percentage.

**None of this applies to census-only questions.** Population or median income for
a tract needs no spatial operation at all — it is already tabular along the GEOID
hierarchy, so those queries use the plain parquet and ignore H3 entirely. That is
the distinction worth keeping in mind before adding more hex data: it buys you
something only when two datasets tessellate differently.

`hex-fractions` versus `hex`: the fractions table has one row per (cell, class)
with `frac` summing to 1 per cell, which is what gives a composition. The `hex`
table has one row per cell with the dominant class and is far smaller — prefer it
for "what cover is this" rather than "what mix".

Sizes: census hex is ~140 MB state, ~124 MB county and 4.8 GB block group;
land-cover hex is 1.09 GB and its fractions 7.05 GB. **Tracts have no hex asset**
— block groups already join to the raster at finer granularity, and a tract's
composition is the aggregate of the block groups sharing its GEOID prefix, so the
6.7 GB bought no query that could not already be written. The block-group table
is 646 million rows over 13.2 million distinct h8 cells, so aggregate against it
and never list rows. `MIRROR_HEX=0` skips all of them.

## The `${PUBLIC_ORIGIN}` placeholder

STAC hrefs are written with a literal `${PUBLIC_ORIGIN}` placeholder rather than
a hostname. The seed job substitutes it at upload time.

This is what lets one bundle serve localhost, a LAN address, or someone else's
reverse proxy with no rebuild — change `PUBLIC_ORIGIN` in `.env`, re-run
`make seed`, done. It also means a bundle can be built on one machine and
shipped to another whose address is not known yet.

The corollary: **`PUBLIC_ORIGIN` must match the URL in the address bar.** If it
does not, `layers-input.json` still loads (its paths are relative) but every
child collection href points somewhere unreachable — the map appears with no
layers and no obvious error. `make verify` checks for exactly this.

## Rebuilding

```bash
make data                                  # all four layers
ONLY_LAYER=us-census-states make data      # one layer
CENSUS_SOURCE_BASE=https://my-mirror/... make data
```

Re-running is cheap and interruption-safe: each file is skipped when its size
already matches the source, and downloads land in `.part` files that are renamed
only on completion — so a half-copied PMTiles archive never gets mistaken for a
finished one. Delete a file in `data/bundle/` to force a re-fetch.

The block-group layer is the slow part at ~1.6 GB across its two assets.

No API key is needed. If you are looking for `CENSUS_API_KEY`, it went away with
the from-scratch pipeline — see "Why mirroring instead of building from source"
above.

## Adding your own data

If the layer already exists in a STAC catalog, add an entry to `LAYERS` in
[`data/mirror.py`](../data/mirror.py) naming its collection and its two asset
keys. The asset keys are not uniform upstream (`state-parquet`,
`blockgroup-parquet`), which is why they are spelled out per layer rather than
pattern-matched.

Otherwise, write whatever you like into `data/bundle/`, as long as it produces:

1. **Parquet** with a geometry column, at a path under `parquet/`.
2. **PMTiles** whose internal layer name you know.
3. **A STAC collection** whose `pmtiles` asset carries
   `"vector:layers": ["<that layer name>"]`.

Point 3 is the one that fails silently. Without `vector:layers`, the app falls
back to using the asset key (`"pmtiles"`) as the source-layer name, MapLibre
finds no such layer in the archive, and **nothing renders, with no error**. If a
layer is missing from the map and everything else looks right, check this first.
`mirror.py` refuses to write a collection whose upstream asset lacks it.

Then add the collection to `catalog.json`'s `child` links, add an entry to
`app/layers-input.json`, and re-run `make seed`.

### Reading what a PMTiles archive actually contains

Use the `pmtiles` CLI rather than parsing the header by hand — the metadata
pointer lives at byte 52 and the block is gzip-compressed, so hand-rolled
readers tend to be quietly wrong. `make basemap` already downloads the binary to
`basemap/bin/pmtiles`:

```bash
basemap/bin/pmtiles show --metadata data/bundle/pmtiles/us-census-blockgroups.pmtiles \
  | python3 -c 'import json,sys
for l in json.load(sys.stdin)["vector_layers"]:
    print(l["id"], sorted(l.get("fields", {})))'
```

The printed `id` must equal the `vector:layers` entry in your STAC asset, and the
printed fields are the only names valid for `tooltip_fields`, `default_filter`
and style expressions. Remember that the mirrored layers use upstream column
names — `MHHI`, not `median_income`.
