# The data bundle

## What gets built

[`data/mirror.py`](../data/mirror.py) copies four already-processed layers out of
the `public-census` STAC catalog and re-points their hrefs at the local object
store:

```
data/bundle/
├── parquet/us-census-{blockgroups,tracts,counties,states}.parquet
├── pmtiles/us-census-{blockgroups,tracts,counties,states}.pmtiles
└── stac/
    ├── catalog.json
    └── us-census-{blockgroups,tracts,counties,states}/collection.json
```

About 3 GB in total. Nothing is computed and **no Census API key is involved.**

**Both forms are required and they are not redundant.** The map reads tiles; the
agent's SQL reads parquet. Tiles carry a subset of attributes at simplified
geometry — they cannot answer an analytical question. Parquet carries full
precision and every column but cannot be rendered. Dropping either silently
removes half the application.

This is also why `tooltip_fields` in `layers-input.json` must be checked against
the **tile** schema, not the parquet schema. A tooltip naming a parquet-only
column renders blank, with no error anywhere. Read the real tile fields with:

```bash
pmtiles show --metadata data/bundle/pmtiles/us-census-blockgroups.pmtiles
```

## Why mirroring instead of building from source

This used to be a from-scratch pipeline: TIGER/Line shapefiles plus ACS API
calls, joined in DuckDB, tiled with tippecanoe, with hand-written STAC on the
end. It produced artifacts that already existed upstream, and it cost:

* a Census API key — and the API stopped serving anonymous queries, signalling it
  with a `302` to an HTML page rather than an error, which surfaced as a bare
  `JSONDecodeError`;
* a build image with GDAL and a from-source tippecanoe compile;
* an hour or more of tiling, plus a class of silent join bugs (vintage
  mismatches, sentinel handling, name-vs-GEOID joins) that had to be defended
  against individually.

Mirroring removes all of it. The upstream collections are the source of truth for
what a layer contains, and `mirror.py` only moves bytes and rewrites hrefs.

The tradeoff is that the bundle now contains what upstream publishes rather than
exactly what we might choose. Which brings us to the important one:

## Demographics are block-group only

The `census-2024` boundary layers carry TIGER attributes and nothing else — no
population, no income. **Every demographic column in this bundle comes from the
ACS 2020–2024 block-group layer**, which is why that is the layer with the
choropleth and the others are boundary context.

| Column | Meaning |
|---|---|
| `TotPop` | Total population |
| `MHHI` | Median household income, dollars. NULL where suppressed; top-coded at 250001 |
| `PCI` | Per-capita income, dollars |
| `PovInd` | **Count** below the poverty threshold — not a rate |
| `Under18`, `Senior65` | Age-group populations |
| `NoCar` | Households with no vehicle available |

Full descriptions ship in each collection's `table:columns`, generated from the
downloaded file itself so the documentation cannot drift from the schema. The
agent reads them via the collection-detail tool.

### Rolling up to county or state

People will ask for median income by county, and the honest answer is that this
bundle cannot give a true one. Medians do not aggregate: you cannot average
block-group medians into a county median. A population-weighted mean is the
usual approximation and it skews high, because it weights by population rather
than by household count and ignores the within-group distribution entirely.

```sql
-- An ESTIMATE, not a median. Label it as such when reporting.
SELECT c.NAMELSAD,
       SUM(b.TotPop) AS pop,
       SUM(b.MHHI * b.TotPop) / NULLIF(SUM(b.TotPop), 0) AS est_mhhi
FROM read_parquet('s3://geo-data/parquet/us-census-blockgroups.parquet') b
JOIN read_parquet('s3://geo-data/parquet/us-census-counties.parquet') c
  ON c.GEOID = substr(b.GEOID, 1, 5)
WHERE b.MHHI IS NOT NULL AND b.TotPop > 0
GROUP BY c.NAMELSAD;
```

Counts — population, households, people below poverty — *do* sum correctly, so
a county poverty rate computed from summed counts is sound. The system prompt
warns the agent about exactly this distinction.

GEOID is hierarchical, which is what makes the join above work: 2 characters for
state, 5 for county, 11 for tract, 12 for block group. Join on GEOID and never
on name — there are dozens of Washington Counties.

## Sentinels, handled upstream

The Census API encodes suppressed estimates as `-666666666`. Left alone, a median
income of minus six hundred million does not merely look wrong — it wrecks every
average, every choropleth break and every "lowest income" ranking while the data
still validates.

The mirrored data has none: `MHHI` contains zero values below `-1000000` and uses
proper NULLs instead (about 19,000 of 242,748 block groups). This is worth
re-checking if you point `CENSUS_SOURCE_BASE` at a different mirror, because the
choropleth in `layers-input.json` assumes it — it maps NULL to a "no data" class
via `coalesce`, and a surviving sentinel would land in the lowest-income bucket
and paint as genuine extreme poverty.

## What is not mirrored

The upstream collections also publish H3 hex assets (`*-hex`). They are skipped:
state and county hex are ~270 MB together, but tract and block-group hex are
~7 GB and ~5 GB, which would quadruple the bundle for something this deployment
does not read — the `/tiles/` hex-pyramid route is served by mcp-data-server out
of its own `tiles` bucket via `register_hex_tiles`. Add them to `LAYERS` in
`mirror.py` if you want them, and update the size figures in the README.

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
