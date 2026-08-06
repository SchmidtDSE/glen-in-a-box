#!/usr/bin/env python3
"""Mirror the pre-processed census layers into the offline data bundle.

Produces, for each layer:

    parquet/<id>.parquet     geometry + attributes, queried by DuckDB via MCP
    pmtiles/<id>.pmtiles     vector tiles, rendered by MapLibre in the browser
    stac/<id>/collection.json

plus a root ``stac/catalog.json`` tying them together.

ONLINE STEP. Run once with ``make data``; the output is what the offline box
actually serves.

Design notes worth knowing before editing
-----------------------------------------

*This copies; it does not compute.* An earlier version of this file rebuilt the
same layers from source — TIGER/Line shapefiles plus ACS API calls, joined in
DuckDB and tiled with tippecanoe. That was several hours, a GDAL + tippecanoe
build image, a Census API key, and a pile of ways to get the join subtly wrong,
all to reproduce artifacts that already exist as cloud-native assets in
``public-census``. So now we mirror them. The upstream STAC is the source of
truth for what a layer contains; this script's only real job is to move bytes
and re-point hrefs at the local object store.

*Nothing here needs an API key.* That is the main reason to prefer mirroring.
The Census API stopped serving anonymous data queries and signals it with a 302
to an HTML page rather than an error, which is a genuinely nasty failure to
diagnose. Mirroring sidesteps it: these buckets are plain public HTTPS.

*Demographics live at block-group level only.* The ``census-2024`` boundary
layers are pure TIGER attributes — no population, no income. Every demographic
column in this bundle comes from ``acs-2020-2024/blockgroup``, which is why the
block-group layer is the one carrying the choropleth and the coarser layers are
boundary context. Do not add a county-level income choropleth without reading
the note in docs/data.md about why medians cannot be rolled up.

*vector:layers is copied from upstream, never guessed.* A ``pmtiles`` asset
without it makes the app fall back to the asset key as the source-layer name,
find no such layer in the archive, and render nothing at all — with no error.
Upstream already records the correct value; this script propagates it and fails
if it is missing.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import duckdb

OUT = Path(os.environ.get("OUT_DIR", "/out"))

# Where the processed layers live. Override to point at a mirror of your own.
SOURCE_BASE = os.environ.get(
    "CENSUS_SOURCE_BASE", "https://s3-west.nrp-nautilus.io/public-census"
)

# Hrefs are emitted with a ${PUBLIC_ORIGIN} placeholder rather than a hostname.
# The seed job substitutes it at upload time, which is what lets one bundle
# serve localhost, a LAN address, or a reverse proxy without a rebuild.
HREF = "${PUBLIC_ORIGIN}/data/geo-data"


# ─────────────────────────────────────────────────────────────────────────────
# What to mirror
# ─────────────────────────────────────────────────────────────────────────────
#
# `source` is a STAC collection under SOURCE_BASE. `parquet` and `pmtiles` name
# the asset keys within it — they are not uniform upstream ("state-parquet",
# "blockgroup-parquet"), which is exactly why they are spelled out here instead
# of being pattern-matched.
#
# The hex assets (`*-hex`) are deliberately NOT mirrored. State and county hex
# are ~270 MB together, but tract and block-group hex are ~7 GB and ~5 GB, which
# would quadruple the bundle for a feature the offline box does not use: the
# hex-pyramid route is served by mcp-data-server from its own `tiles` bucket via
# register_hex_tiles, not from these files. Add them here if you want them, and
# expect the bundle size in the README to stop being true.

LAYERS = [
    {
        "id": "us-census-blockgroups",
        "source": "acs-2020-2024/blockgroup/stac-collection.json",
        "parquet": "blockgroup-parquet",
        "pmtiles": "blockgroup-pmtiles",
        "title": "ACS Block Groups (2020–2024)",
        "description": (
            "Census block-group boundaries carrying American Community Survey "
            "2020–2024 five-year demographic estimates. This is the only layer "
            "in the bundle with population and income attributes; the state, "
            "county and tract layers are boundaries only. Block groups are the "
            "finest geography here and their estimates carry the widest margins "
            "of error — treat small differences between neighbouring block "
            "groups as noise."
        ),
    },
    {
        "id": "us-census-tracts",
        "source": "census-2024/tract/stac-collection.json",
        "parquet": "tract-parquet",
        "pmtiles": "tract-pmtiles",
        "title": "US Census Tracts (2024)",
        "description": (
            "Census tract boundaries from TIGER/Line 2024. Boundaries and TIGER "
            "identifiers only — no demographic attributes. Join to the "
            "block-group layer on the leading 11 characters of GEOID to "
            "summarise demographics by tract."
        ),
    },
    {
        "id": "us-census-counties",
        "source": "census-2024/county/stac-collection.json",
        "parquet": "county-parquet",
        "pmtiles": "county-pmtiles",
        "title": "US Counties (2024)",
        "description": (
            "County and county-equivalent boundaries from TIGER/Line 2024. "
            "Boundaries and TIGER identifiers only — no demographic attributes. "
            "Join to the block-group layer on the leading 5 characters of GEOID."
        ),
    },
    {
        "id": "us-census-states",
        "source": "census-2024/state/stac-collection.json",
        "parquet": "state-parquet",
        "pmtiles": "state-pmtiles",
        "title": "US States (2024)",
        "description": (
            "State and state-equivalent boundaries from TIGER/Line 2024, "
            "including DC and Puerto Rico. Boundaries and TIGER identifiers "
            "only — no demographic attributes. Join to the block-group layer on "
            "the leading 2 characters of GEOID."
        ),
    },
]


# Descriptions for the columns that carry meaning. Anything not listed here gets
# a generic note rather than being dropped: the agent reads this via get_schema,
# and a column it cannot see is a column it will never query, but a column with
# a vague description is merely unhelpful.
#
# The ACS names are abbreviated upstream and not self-explanatory, which is the
# main reason this table exists at all.
COLUMN_DOCS = {
    # Identifiers
    "GEOID": "Census geographic identifier. The join key — names are not unique. "
             "Hierarchical: the first 2 characters are the state, 5 the county, "
             "11 the tract, 12 the block group.",
    "GEOIDFQ": "Fully-qualified GEOID including the Census summary-level prefix.",
    "NAME": "Short geography name. For tracts and block groups this is a bare "
            "number; prefer NAMELSAD for display.",
    "NAMELSAD": "Geography name including its legal/statistical description, "
                "e.g. 'Alameda County' or 'Block Group 1'.",
    "STUSPS": "Two-letter state abbreviation (state layer only).",
    "STATEFP": "State FIPS code.",
    "COUNTYFP": "County FIPS code, unique only within a state.",
    "TRACTCE": "Tract code, unique only within a county.",
    "BLKGRPCE": "Block-group code, unique only within a tract.",
    # ACS demographics
    "TotPop": "Total population (ACS five-year estimate).",
    "MHHI": "Median household income in dollars (ACS five-year estimate). NULL "
            "where the estimate was suppressed — never zero. Top-coded at "
            "250001, which is a ceiling, not a real value.",
    "PCI": "Per-capita income in dollars (ACS five-year estimate).",
    "PovInd": "Count of people for whom poverty status is determined and who "
              "fall below the threshold. A COUNT, not a rate — divide by "
              "TotPop for a rate, and guard against TotPop = 0.",
    "Under18": "Population under 18 years old.",
    "Senior65": "Population 65 years and over.",
    "NoCar": "Households with no vehicle available.",
    # Geometry and area
    "geom": "Polygon boundary, EPSG:4326.",
    "bbox": "Bounding box of the geometry, as a struct of xmin/ymin/xmax/ymax.",
    "ALAND": "Land area in square metres (TIGER ALAND).",
    "AWATER": "Water area in square metres (TIGER AWATER).",
    "INTPTLAT": "Latitude of an internal point, as a string.",
    "INTPTLON": "Longitude of an internal point, as a string.",
}

GENERIC_DOC = "TIGER/Line attribute carried through from the source file."


def log(msg: str) -> None:
    print(msg, flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Fetch
# ─────────────────────────────────────────────────────────────────────────────

def fetch_json(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"\nHTTP {e.code} fetching {url}\n{e.reason}") from e
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"\n{url} did not return JSON. If this is an HTML page, the source "
            f"base may be wrong:\n  CENSUS_SOURCE_BASE={SOURCE_BASE}\n"
        ) from e


def remote_size(url: str) -> int | None:
    """Content-Length via HEAD, or None if the server will not say."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            n = r.headers.get("Content-Length")
            return int(n) if n else None
    except urllib.error.HTTPError:
        return None


def download(url: str, dest: Path) -> Path:
    """Fetch to `dest`, skipping when a complete copy is already there.

    The size comparison is what makes `make data` cheap to re-run and safe to
    interrupt. Writing to .part and renaming only on success is what stops an
    interrupted transfer from leaving a truncated file that the next run then
    treats as complete — a half-copied PMTiles archive is a layer that renders
    for a while and then stops, which is a miserable thing to debug.
    """
    expected = remote_size(url)
    if dest.exists() and expected is not None and dest.stat().st_size == expected:
        log(f"    have  {dest.name} ({expected / 1e6:.0f} MB)")
        return dest
    if dest.exists() and expected is None and dest.stat().st_size > 0:
        log(f"    have  {dest.name} (size unverified — server sent no length)")
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    size_note = f" ({expected / 1e6:.0f} MB)" if expected else ""
    log(f"    GET   {dest.name}{size_note}")

    try:
        with urllib.request.urlopen(url, timeout=300) as r, tmp.open("wb") as f:
            done = 0
            last = -1
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if expected:
                    pct = int(100 * done / expected)
                    if pct >= last + 10:
                        log(f"          {pct}%")
                        last = pct
    except urllib.error.HTTPError as e:
        raise SystemExit(f"\nHTTP {e.code} fetching {url}\n{e.reason}") from e

    if expected is not None and tmp.stat().st_size != expected:
        tmp.unlink(missing_ok=True)
        raise SystemExit(
            f"\n{dest.name}: expected {expected} bytes, got {tmp.stat().st_size}. "
            f"Transfer was truncated; re-run to retry."
        )

    tmp.rename(dest)
    return dest


# ─────────────────────────────────────────────────────────────────────────────
# STAC
# ─────────────────────────────────────────────────────────────────────────────

def describe_columns(con: duckdb.DuckDBPyConnection, parquet: Path) -> list[dict]:
    """Build table:columns from the file itself, not from an assumption.

    Read from the local copy after download, so what the agent is told matches
    what it will actually query. Deriving this from a hardcoded list is how a
    schema and its documentation drift apart.
    """
    rows = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{parquet.as_posix()}')"
    ).fetchall()
    return [
        {
            "name": name,
            "type": dtype.lower(),
            "description": COLUMN_DOCS.get(name, GENERIC_DOC),
        }
        for name, dtype, *_ in rows
    ]


def write_stac(layer: dict, source: dict, columns: list[dict],
               vector_layers: list[str], source_url: str) -> None:
    cid = layer["id"]
    d = OUT / "stac" / cid
    d.mkdir(parents=True, exist_ok=True)

    collection = {
        "type": "Collection",
        "stac_version": "1.0.0",
        "stac_extensions": [
            "https://stac-extensions.github.io/table/v1.2.0/schema.json",
        ],
        "id": cid,
        "title": layer["title"],
        "description": layer["description"],
        # Carried from upstream where present rather than restated, so a change
        # in licence or coverage travels with the data.
        "license": source.get("license", "CC0-1.0"),
        "providers": source.get("providers") or [
            {"name": "US Census Bureau", "roles": ["producer", "licensor"],
             "url": "https://www.census.gov/"},
        ],
        "extent": source.get("extent", {
            "spatial": {"bbox": [[-180.0, 15.0, -64.0, 72.0]]},
            "temporal": {"interval": [[None, None]]},
        }),
        "links": [
            {"rel": "root", "href": f"{HREF}/stac/catalog.json", "type": "application/json"},
            {"rel": "parent", "href": f"{HREF}/stac/catalog.json", "type": "application/json"},
            {"rel": "self", "href": f"{HREF}/stac/{cid}/collection.json", "type": "application/json"},
            # Provenance: where these bytes came from. Not resolvable offline,
            # and not meant to be — it records what to re-mirror from.
            {"rel": "via", "href": source_url, "type": "application/json",
             "title": "Source collection this was mirrored from"},
        ],
        "table:columns": columns,
        "assets": {
            "pmtiles": {
                "href": f"{HREF}/pmtiles/{cid}.pmtiles",
                "type": "application/vnd.pmtiles",
                "title": f"{layer['title']} vector tiles",
                "roles": ["visual", "tiles"],
                # Copied from upstream. Without it the app uses the asset key
                # ("pmtiles") as the source-layer name, finds no such layer in
                # the archive, and renders nothing — with no error.
                "vector:layers": vector_layers,
            },
            "parquet": {
                "href": f"{HREF}/parquet/{cid}.parquet",
                "type": "application/vnd.apache.parquet",
                "title": f"{layer['title']} table",
                "roles": ["data"],
                "table:columns": columns,
            },
        },
    }
    (d / "collection.json").write_text(json.dumps(collection, indent=2))
    log(f"    stac/{cid}/collection.json ({len(columns)} columns documented)")


def write_catalog() -> None:
    catalog = {
        "type": "Catalog",
        "stac_version": "1.0.0",
        "id": "glen-in-a-box",
        "title": "Glen in a Box — offline US census catalog",
        "description": (
            "US Census Bureau geography and American Community Survey estimates, "
            "mirrored for fully offline use. Every asset is served from the local "
            "object store; nothing here resolves to the internet."
        ),
        "links": [
            {"rel": "root", "href": f"{HREF}/stac/catalog.json", "type": "application/json"},
            {"rel": "self", "href": f"{HREF}/stac/catalog.json", "type": "application/json"},
            *[
                {
                    "rel": "child",
                    "href": f"{HREF}/stac/{lyr['id']}/collection.json",
                    "type": "application/json",
                    "title": lyr["title"],
                }
                for lyr in LAYERS
            ],
        ],
    }
    (OUT / "stac").mkdir(parents=True, exist_ok=True)
    (OUT / "stac" / "catalog.json").write_text(json.dumps(catalog, indent=2))
    log("    stac/catalog.json")


# ─────────────────────────────────────────────────────────────────────────────

def asset_href(source: dict, key: str, source_url: str) -> dict:
    assets = source.get("assets") or {}
    if key not in assets:
        raise SystemExit(
            f"\n{source_url}\n  has no asset '{key}'. Available: "
            f"{sorted(assets)}\n  Update LAYERS in data/mirror.py."
        )
    return assets[key]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    only = os.environ.get("ONLY_LAYER", "").strip()
    layers = LAYERS
    if only:
        layers = [l for l in LAYERS if l["id"] == only]
        if not layers:
            raise SystemExit(
                f"ONLY_LAYER={only!r} matches nothing. Choose from: "
                f"{[l['id'] for l in LAYERS]}"
            )

    con = duckdb.connect()

    log(f"==> mirroring from {SOURCE_BASE}")

    for layer in layers:
        cid = layer["id"]
        source_url = f"{SOURCE_BASE}/{layer['source']}"
        log(f"\n==> {cid}")
        log(f"    source {source_url}")
        source = fetch_json(source_url)

        pq_asset = asset_href(source, layer["parquet"], source_url)
        mv_asset = asset_href(source, layer["pmtiles"], source_url)

        vector_layers = mv_asset.get("vector:layers")
        if not vector_layers:
            raise SystemExit(
                f"\n{source_url}\n  asset '{layer['pmtiles']}' has no "
                f"'vector:layers'. Without it the map renders nothing and says "
                f"nothing. Read the layer name out of the archive with\n"
                f"    pmtiles show --metadata <url>\n  and hardcode it here."
            )

        pq = download(pq_asset["href"], OUT / "parquet" / f"{cid}.parquet")
        download(mv_asset["href"], OUT / "pmtiles" / f"{cid}.pmtiles")

        columns = describe_columns(con, pq)
        write_stac(layer, source, columns, vector_layers, source_url)

    write_catalog()

    log("\n==> bundle complete")
    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    log(f"    {OUT}: {total / 1e9:.2f} GB")
    log("\nNext: make up && make seed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
