# The data bundle

## What gets built

[`data/build.py`](../data/build.py) turns TIGER/Line geometry and ACS estimates
into three layers, each written in two forms:

```
data/bundle/
├── parquet/us-census-{states,counties,tracts}.parquet
├── pmtiles/us-census-{states,counties,tracts}.pmtiles
└── stac/
    ├── catalog.json
    └── us-census-{states,counties,tracts}/collection.json
```

**Both forms are required and they are not redundant.** The map reads tiles;
the agent's SQL reads parquet. Tiles carry only the handful of attributes
tippecanoe was told to keep, at simplified geometry — they cannot answer an
analytical question. Parquet carries full precision and every column but cannot
be rendered. Dropping either silently removes half the application.

This is also why `tooltip_fields` in `layers-input.json` must be checked
against the **tile** schema, not the parquet schema. The authoritative list is
`TILE_FIELDS` in `build.py`. A tooltip naming a parquet-only column renders
blank, with no error anywhere.

## Census gotchas the pipeline handles

Two of these will bite anyone writing a census pipeline from scratch.

**Suppressed values are large negative sentinels, not nulls.** The Census API
returns `-666666666` where an estimate is suppressed. Left alone, a median
income of minus six hundred million does not merely look wrong — it silently
wrecks every average, every choropleth break, and every "lowest income"
ranking, while the data still validates. `build.py` nulls anything below
`SENTINEL_FLOOR`.

**Names are not join keys.** There are dozens of Washington Counties. Every
join is on GEOID, built by concatenating the geography key columns in hierarchy
order: state, then state+county, then state+county+tract.

Two more the pipeline handles quietly: `poverty_rate` guards its denominator,
because unpopulated tracts have a zero universe and `0/0` produces a NaN that
parquet carries happily and every consumer then mishandles differently. And
`STUSPS` exists only on the state shapefile, so counties and tracts get it by
joining back on the first two GEOID digits — downstream SQL never has to know.

Things the pipeline cannot fix, and which the system prompt therefore warns the
agent about: ACS figures are five-year estimates with margins of error that get
wide for small geographies, and medians never aggregate — you cannot average
county medians into a state median.

## Tile design

`TILE_FIELDS` is deliberately short. Every attribute is paid for in every tile
at every zoom, forever, so tiles get only what styling and hover need and
everything else stays in parquet.

Zoom ranges are matched to use. Tracts start at z5: 85,000 of them at z0–4 is a
vast amount of data for a view in which no single tract is a pixel.

tippecanoe runs with `--no-feature-limit --no-tile-size-limit`. The defaults
drop features to hit a size budget, which for census geography means whole
counties silently vanishing at low zoom — worse than a larger tile.

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
make data                          # everything
ONLY_LEVEL=counties make data      # one layer
TIGER_YEAR=2023 ACS_YEAR=2022 make data
```

`data/cache/` holds downloaded TIGER archives and ACS responses, so a re-run
after a failed tile build does not re-download 56 shapefiles. Delete it to force
a clean fetch.

Tracts are the slow part: 56 shapefile downloads, 56 API calls, and roughly
85,000 features through tippecanoe. Budget half an hour or more.

`CENSUS_API_KEY` is **required**, and free — issued instantly at
[api.census.gov/data/key_signup.html](https://api.census.gov/data/key_signup.html).
Put it in `.env`:

```
CENSUS_API_KEY=your_key_here
```

The API no longer answers anonymous data queries. It does not return an HTTP
error either: it replies `302` to an HTML page at `/data/missing_key.html`, so
the first thing to notice used to be `json.loads` failing with
`Expecting value: line 1 column 1`. `build.py` now checks for the key before it
downloads anything, and detects the redirect if the key is present but rejected.

## Adding your own data

Write anything you like into `data/bundle/`, as long as it produces:

1. **Parquet** with a geometry column, at a path under `parquet/`.
2. **PMTiles** whose internal layer name you know — it is tippecanoe's `-l`
   argument.
3. **A STAC collection** whose `pmtiles` asset carries
   `"vector:layers": ["<that layer name>"]`.

Point 3 is the one that fails silently. Without `vector:layers`, the app falls
back to using the asset key (`"pmtiles"`) as the source-layer name, MapLibre
finds no such layer in the archive, and **nothing renders, with no error**. If a
layer is missing from the map and everything else looks right, check this first.

Then add the collection to `catalog.json`'s `child` links, add an entry to
`app/layers-input.json`, and re-run `make seed`.

To verify what a PMTiles archive actually contains:

```bash
docker run --rm -v "$PWD/data/bundle:/d" python:3.12-slim python3 - <<'EOF'
import struct, json
with open('/d/pmtiles/us-census-states.pmtiles','rb') as f:
    head = f.read(16384)
    off, ln = struct.unpack_from('<Q', head, 24)[0], struct.unpack_from('<Q', head, 32)[0]
    f.seek(off); meta = json.loads(f.read(ln))
for layer in meta.get('vector_layers', []):
    print(layer['id'], sorted(layer.get('fields', {})))
EOF
```

The printed `id` must equal the `vector:layers` entry in your STAC asset, and
the printed fields are the only names valid for `tooltip_fields` and
`default_filter`.
