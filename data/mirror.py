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

# The H3 assets are the bulk of the bundle (~12.5 GB of ~15.6). Set MIRROR_HEX=0
# for a boundaries-and-attributes-only bundle, and expect hex/density
# visualisations to need an H3 index synthesised per request.
MIRROR_HEX = os.environ.get("MIRROR_HEX", "1").strip().lower() not in ("0", "false", "no")

# Default mode. The data objects are byte-identical copies — only the STAC needs
# rewriting — so there is no reason to stage 15 GB on local disk just to upload
# the same 15 GB into rustfs. In STAC_ONLY mode this writes the catalog plus a
# manifest of source->destination keys, and the seed job mirrors the objects
# bucket-to-bucket. One transfer, one stored copy.
#
# Set STAC_ONLY=0 to materialise the whole bundle locally instead, which is what
# you want when building an air-gap payload to carry to a disconnected machine.
STAC_ONLY = os.environ.get("STAC_ONLY", "1").strip().lower() not in ("0", "false", "no")

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
# `hex` names the H3-indexed asset. Those are hive-partitioned directories
# (`hex/h0=<cell>/data_0.parquet`), not single files, so they are listed and
# copied partition by partition — see mirror_hex_prefix.
#
# They are large: ~140 MB state, ~130 MB county, ~7.1 GB tract, ~5.1 GB
# block group, so mirroring all four takes the bundle from ~3 GB to ~15.6 GB.
# They are mirrored anyway, because they are the whole point of having a
# pre-processed catalog: register_hex_tiles wants SQL whose first column is an
# H3 index, and without these the agent has to synthesise one from interior-point
# coordinates on every request. Set MIRROR_HEX=0 to skip them and accept that.

LAYERS = [
    {
        "id": "us-census-blockgroups",
        "source": "acs-2020-2024/blockgroup/stac-collection.json",
        "parquet": "blockgroup-parquet",
        "pmtiles": "blockgroup-pmtiles",
        "hex": "blockgroup-hex",
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
        # No hex for tracts, deliberately. The H3 indexes exist for
        # cross-tessellation joins against the raster, and block groups already
        # do that at finer granularity — a tract's land-cover composition is the
        # aggregate of its block groups, which share its GEOID prefix. Mirroring
        # it too cost 6.7 GB and bought no query that could not already be
        # written. Add "hex": "tract-hex" back if you find one.
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
        "hex": "county-hex",
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
        "hex": "state-hex",
        "title": "US States (2024)",
        "description": (
            "State and state-equivalent boundaries from TIGER/Line 2024, "
            "including DC and Puerto Rico. Boundaries and TIGER identifiers "
            "only — no demographic attributes. Join to the block-group layer on "
            "the leading 2 characters of GEOID."
        ),
    },
]


# Raster layers. Kept separate from LAYERS because nothing about them is tabular:
# there is no parquet, no vector tiles and no schema to document — just a COG that
# TiTiler renders.
#
# NLCD is a good fit for this box specifically: it is CONUS-only, and its STAC
# asset carries `classification:classes` with a colour per class, so geo-agent
# builds the real NLCD palette and labels the legend itself rather than needing a
# hand-maintained copy here.
RASTERS = [
    {
        "id": "us-nlcd-2024",
        "source": "public-land-cover/nlcd-2024/stac-collection.json",
        "asset": "nlcd-2024-cog",
        "dest": "cog/nlcd-2024.tif",
        # This is what makes the H3 indexes worth their disk. A raster and census
        # polygons tessellate space differently, so "land cover of this county"
        # is normally a spatial operation. Index both to the same H3 cells and it
        # collapses to a join on h10 — no geometry math, and an 8B model can
        # write it. Nothing analogous is needed for population per tract, which
        # is already tabular along the census hierarchy.
        "hexes": [
            {"asset": "nlcd-2024-hex-fractions", "sub": "hex-fractions",
             "title": "class fractions per H3 cell",
             "note": "One row per (cell, class) with `frac`, the fraction of the "
                     "cell covered by that class. Use this for COMPOSITION — the "
                     "mix of land cover within a tract or county. Fractions for a "
                     "cell sum to 1."},
            {"asset": "nlcd-2024-hex", "sub": "hex",
             "title": "dominant class per H3 cell",
             "note": "One row per cell with the single dominant class in `nlcd`. "
                     "Much smaller than the fractions table, so prefer it when the "
                     "question is 'what cover is this' rather than 'what mix'."},
        ],
        "title": "NLCD Land Cover 2024 (CONUS)",
        "description": (
            "Annual National Land Cover Database, 2024, 30 m, conterminous US. "
            "A categorical raster: each pixel is a land-cover class, so it is "
            "rendered by TiTiler rather than queried as a table. Useful alongside "
            "the census layers — what land cover surrounds a given block group — "
            "but it holds no attributes to join on."
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
    # Land cover
    "nlcd": "NLCD land-cover class code. Join to the class names and colours in "
            "the us-nlcd-2024 collection's raster:bands classification:classes.",
    "frac": "Fraction of the H3 cell covered by this land-cover class, 0-1. "
            "Fractions within a cell sum to 1, so a population- or area-weighted "
            "mean over cells gives a composition.",
    # H3 indexes, present only in the hex assets
    "h0": "H3 cell at resolution 0 — the hive partition key. Filter on it to let "
          "DuckDB skip partitions.",
    "h5": "H3 cell index at resolution 5 (~250 km² per cell).",
    "h6": "H3 cell index at resolution 6 (~36 km² per cell).",
    "h7": "H3 cell index at resolution 7 (~5 km² per cell).",
    "h8": "H3 cell index at resolution 8 (~0.7 km² per cell).",
    "h9": "H3 cell index at resolution 9 (~0.1 km² per cell).",
    "h10": "H3 cell index at resolution 10 (~0.015 km² per cell). Finest here.",
    # Geometry and area
    "geom": "Polygon boundary, EPSG:4326. NEVER put this column bare in a SELECT "
            "list: the server strips geometry from results, so `SELECT geom` "
            "becomes an empty select list and fails with 'Parser Error: SELECT "
            "clause without selection list', and `SELECT GEOID, geom` silently "
            "returns only GEOID. Use it inside a function instead — "
            "ST_X(ST_Centroid(geom)), ST_AsText(geom), or in a WHERE clause — "
            "which all work normally.",
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

    # The .part can vanish under us if a second mirror is running against the
    # same bundle — it renames the file into place while this one is still
    # writing, and the stat below then dies with a bewildering FileNotFoundError
    # at 99%. Do not run two mirrors on one bundle; but if it happens, notice
    # what the other process accomplished rather than crashing.
    if not tmp.exists():
        if dest.exists() and (expected is None or dest.stat().st_size == expected):
            log(f"    have  {dest.name} (completed by another process)")
            return dest
        raise SystemExit(
            f"\n{dest.name}: the partial download disappeared mid-transfer.\n"
            f"  Another `make data` is probably running against {OUT} — run one "
            f"at a time."
        )

    if expected is not None and tmp.stat().st_size != expected:
        tmp.unlink(missing_ok=True)
        raise SystemExit(
            f"\n{dest.name}: expected {expected} bytes, got {tmp.stat().st_size}. "
            f"Transfer was truncated; re-run to retry."
        )

    tmp.rename(dest)
    return dest


def list_prefix(bucket_url: str, prefix: str) -> list[tuple[str, int]]:
    """List (key, size) under a prefix via S3 ListObjectsV2, following pages."""
    import xml.etree.ElementTree as ET
    ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
    out: list[tuple[str, int]] = []
    token = None
    while True:
        url = f"{bucket_url}?list-type=2&prefix={urllib.parse.quote(prefix)}&max-keys=1000"
        if token:
            url += "&continuation-token=" + urllib.parse.quote(token)
        with urllib.request.urlopen(url, timeout=120) as r:
            root = ET.fromstring(r.read())
        for c in root.findall(f"{ns}Contents"):
            key = c.findtext(f"{ns}Key")
            size = int(c.findtext(f"{ns}Size") or 0)
            if key and size > 0:
                out.append((key, size))
        if (root.findtext(f"{ns}IsTruncated") or "false").lower() != "true":
            break
        token = root.findtext(f"{ns}NextContinuationToken")
        if not token:
            break
    return out


def hex_sample_url(hex_href: str) -> str:
    """URL of the smallest partition under a hex prefix, for a cheap schema read.

    The partitions share a schema and the largest runs past a gigabyte, so pick
    the smallest rather than whatever the glob happens to match first.
    """
    base, bucket_url, prefix = _split_hex_href(hex_href)
    objs = list_prefix(bucket_url, prefix)
    if not objs:
        raise SystemExit(f"\nno objects under {bucket_url}/{prefix}")
    key = min(objs, key=lambda kv: kv[1])[0]
    return f"{bucket_url}/{urllib.parse.quote(key)}"


def _split_hex_href(hex_href: str) -> tuple[str, str, str]:
    """Split a hex glob href into (base, bucket_url, prefix)."""
    marker = "/hex/"
    if marker not in hex_href:
        raise SystemExit(f"\nhex href has no /hex/ segment: {hex_href}")
    base, _ = hex_href.split(marker, 1)
    scheme, rest = base.split("://", 1)
    host, bucket_and_path = rest.split("/", 1)
    bucket = bucket_and_path.split("/", 1)[0]
    bucket_url = f"{scheme}://{host}/{bucket}"
    prefix = (bucket_and_path.split("/", 1)[1] + marker) if "/" in bucket_and_path else marker
    return base, bucket_url, prefix.lstrip("/")


def mirror_hex_prefix(cid: str, hex_href: str) -> int:
    """Copy an H3 partition directory, preserving its hive layout.

    The upstream href is a glob (`.../hex/h0=*/data_0.parquet`), which names a
    directory of partitions rather than one object, so it cannot simply be
    downloaded. Strip back to the prefix, list it, and mirror each partition to
    the same relative path locally so a `read_parquet` glob still resolves and
    DuckDB can still prune by h0.
    """
    marker = "/hex/"
    _, bucket_url, prefix = _split_hex_href(hex_href)

    objs = list_prefix(bucket_url, prefix)
    if not objs:
        raise SystemExit(
            f"\n{cid}: no objects under {bucket_url}/{prefix}\n"
            f"  The hex asset href was {hex_href}"
        )
    total = sum(s for _, s in objs)
    log(f"    hex: {len(objs)} partition(s), {total / 1e9:.2f} GB")
    for key, _ in sorted(objs):
        # Keep everything from h0=... onward, so the local layout matches.
        rel = key.split(marker, 1)[1]
        download(f"{bucket_url}/{urllib.parse.quote(key)}",
                 OUT / "hex" / cid / rel)
    return total


# ─────────────────────────────────────────────────────────────────────────────
# STAC
# ─────────────────────────────────────────────────────────────────────────────

def describe_columns(con: duckdb.DuckDBPyConnection, parquet: str) -> list[dict]:
    """Build table:columns from the file itself, not from an assumption.

    Read from the local copy after download, so what the agent is told matches
    what it will actually query. Deriving this from a hardcoded list is how a
    schema and its documentation drift apart.
    """
    rows = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{parquet}')"
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
               vector_layers: list[str], source_url: str,
               hex_columns: list[dict] | None = None) -> None:
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
            # No `via` link back to the source. It seems harmless — provenance
            # that simply will not resolve offline — but the MCP server rewrites
            # every https href in a collection to an s3:// path for DuckDB, so a
            # link to s3-west.nrp-nautilus.io became `s3://public-census` in the
            # agent's context. The agent then queried THAT, failed on
            # credentials, and asked the user for AWS keys. A URL in a served
            # file is a run-phase URL no matter how documentary it looks.
            # Provenance lives in stac/MIRROR-PROVENANCE.json instead, which is
            # not part of the catalog graph and so never reaches the agent.
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

    # The H3-indexed copy, when mirrored. The href is a glob because this is a
    # hive-partitioned directory, and DuckDB reads it as one table — the h0
    # partition key lets it prune. This is what register_hex_tiles wants: its SQL
    # needs an H3 index as the first column, and here one already exists.
    if hex_columns is not None:
        collection["assets"]["hex"] = {
            "href": f"{HREF}/hex/{cid}/h0=*/data_0.parquet",
            "type": "application/vnd.apache.parquet",
            "title": f"{layer['title']} indexed to H3 cells",
            "roles": ["data", "index"],
            "description": (
                "Same attributes as the parquet asset, exploded to H3 cells: one "
                "row per cell per feature. h8/h9/h10 are H3 indexes at those "
                "resolutions and h0 is the partition key. Use this for hex, "
                "density and heatmap visualisations — pass an H3 column as the "
                "FIRST column to register_hex_tiles. Row counts are much larger "
                "than the parquet asset, so aggregate rather than listing rows."
            ),
            "table:columns": hex_columns,
        }
    (d / "collection.json").write_text(json.dumps(collection, indent=2))
    log(f"    stac/{cid}/collection.json ({len(columns)} columns documented)")


def write_manifest(entries: list[dict]) -> None:
    """Write the hydration plan the seed job executes.

    TSV, not JSON, because seed/entrypoint.sh is POSIX shell with no JSON parser
    — the same reasoning as app/vendor/manifest.tsv. One line per object:

        dest<TAB>size<TAB>src

    Source URLs live ONLY here. They are external, and this file is deliberately
    not part of the STAC catalog: anything reachable from the catalog gets
    rewritten into the agent's context, and an agent that can see an upstream
    bucket will try to query it.
    """
    lines = [
        "# Hydration plan for seed/entrypoint.sh — see data/mirror.py.",
        "# dest<TAB>size<TAB>src   (source objects are copied bucket-to-bucket)",
        f"# source_base: {SOURCE_BASE}",
        f"# total: {sum(e.get('size') or 0 for e in entries) / 1e9:.2f} GB "
        f"across {len(entries)} objects",
    ]
    for e in entries:
        lines.append(f"{e['dest']}\t{e.get('size') or 0}\t{e['src']}")
    (OUT / "HYDRATE-MANIFEST.tsv").write_text("\n".join(lines) + "\n")
    total = sum(e.get("size") or 0 for e in entries) / 1e9
    log(f"    HYDRATE-MANIFEST.tsv ({len(entries)} objects, {total:.2f} GB to mirror)")


def write_provenance(layers: list[dict]) -> None:
    """Record what was mirrored from where, OUTSIDE the catalog graph.

    Deliberately not a STAC link: see write_stac. Nothing reads this at run time;
    it exists so a human can tell what to re-mirror and from which vintage.
    """
    doc = {
        "_comment": "Provenance for the mirrored bundle. NOT part of the STAC "
                    "catalog: these URLs are external, and anything reachable "
                    "from the catalog gets rewritten into the agent's context.",
        "source_base": SOURCE_BASE,
        "layers": {l["id"]: f"{SOURCE_BASE}/{l['source']}" for l in layers},
    }
    (OUT / "stac" / "MIRROR-PROVENANCE.json").write_text(json.dumps(doc, indent=2))
    log("    stac/MIRROR-PROVENANCE.json (not linked from the catalog)")


def write_raster_stac(raster: dict, source: dict, asset: dict,
                      hex_assets: list[dict] | None = None) -> None:
    """Write a collection for a single COG.

    The asset href is an `s3://` path, NOT ${PUBLIC_ORIGIN}, and that is
    deliberate. The browser hands this href to TiTiler as `?url=`, and TiTiler
    resolves it server-side using its own S3 configuration. A public-origin URL
    would send TiTiler back out through nginx — which, behind an authenticating
    proxy, it cannot get through, so every tile would 401 while the rest of the
    app worked.
    """
    cid = raster["id"]
    d = OUT / "stac" / cid
    d.mkdir(parents=True, exist_ok=True)

    a = {
        "href": f"s3://geo-data/{raster['dest']}",
        "type": asset.get("type", "image/tiff; application=geotiff; profile=cloud-optimized"),
        "title": raster["title"],
        "roles": asset.get("roles") or ["data", "visual"],
    }
    # Carried verbatim: the class values, names and colours are what make the map
    # legible and the legend self-labelling.
    if asset.get("raster:bands"):
        a["raster:bands"] = asset["raster:bands"]

    collection = {
        "type": "Collection",
        "stac_version": "1.0.0",
        "stac_extensions": [
            "https://stac-extensions.github.io/raster/v1.1.0/schema.json",
        ],
        "id": cid,
        "title": raster["title"],
        "description": raster["description"],
        "license": source.get("license", "proprietary"),
        "providers": source.get("providers") or [],
        "extent": source.get("extent", {
            "spatial": {"bbox": [[-180.0, 15.0, -64.0, 72.0]]},
            "temporal": {"interval": [[None, None]]},
        }),
        "links": [
            {"rel": "root", "href": f"{HREF}/stac/catalog.json", "type": "application/json"},
            {"rel": "parent", "href": f"{HREF}/stac/catalog.json", "type": "application/json"},
            {"rel": "self", "href": f"{HREF}/stac/{cid}/collection.json", "type": "application/json"},
        ],
        "assets": {"cog": a},
    }

    for h in (hex_assets or []):
        collection["assets"][h["key"]] = {
            "href": f"{HREF}/{h['dest_prefix']}h0=*/data_0.parquet",
            "type": "application/vnd.apache.parquet",
            "title": f"{raster['title']} — {h['title']}",
            "roles": ["data", "index"],
            "description": h["note"] + (
                " Joins to the census hex assets on h10 (or a coarser h8/h9), "
                "which is how you get land cover per census geography without a "
                "spatial operation."
            ),
            "table:columns": h["columns"],
        }
    (d / "collection.json").write_text(json.dumps(collection, indent=2))
    n = len(((asset.get("raster:bands") or [{}])[0]).get("classification:classes") or [])
    log(f"    stac/{cid}/collection.json ({n} land-cover classes carried)")


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
                    "href": f"{HREF}/stac/{item['id']}/collection.json",
                    "type": "application/json",
                    "title": item["title"],
                }
                for item in (*LAYERS, *RASTERS)
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
    if STAC_ONLY:
        con.execute("INSTALL httpfs; LOAD httpfs;")
    manifest: list[dict] = []

    log(f"==> mirroring from {SOURCE_BASE}")
    log(f"    mode: {'STAC only (seed hydrates bucket-to-bucket)' if STAC_ONLY else 'full local bundle'}")

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

        hex_asset = None
        if MIRROR_HEX and layer.get("hex"):
            hex_asset = asset_href(source, layer["hex"], source_url)

        if STAC_ONLY:
            # Schemas come from the source over HTTP. DuckDB reads only the
            # parquet footer for a DESCRIBE, so this is a few KB per file, not
            # a download.
            columns = describe_columns(con, pq_asset["href"])
            for asset, dest in ((pq_asset, f"parquet/{cid}.parquet"),
                                (mv_asset, f"pmtiles/{cid}.pmtiles")):
                manifest.append({"kind": "object", "src": asset["href"],
                                 "dest": dest,
                                 "size": remote_size(asset["href"]) or 0})
            hex_columns = None
            if hex_asset:
                # Expand the partition directory into explicit objects. The seed
                # job then needs no source credentials and no listing logic of
                # its own — it just streams each URL to a key.
                _, bucket_url, prefix = _split_hex_href(hex_asset["href"])
                objs = list_prefix(bucket_url, prefix)
                if not objs:
                    raise SystemExit(f"\n{cid}: no hex objects under {prefix}")
                total = sum(sz for _, sz in objs)
                log(f"    hex: {len(objs)} partition(s), {total / 1e9:.2f} GB "
                    f"(mirrored by seed, not downloaded here)")
                for key, sz in sorted(objs):
                    rel = key.split("/hex/", 1)[1]
                    manifest.append({
                        "kind": "object", "size": sz,
                        "src": f"{bucket_url}/{urllib.parse.quote(key)}",
                        "dest": f"hex/{cid}/{rel}",
                    })
                hex_columns = describe_columns(
                    con, f"{bucket_url}/{urllib.parse.quote(min(objs, key=lambda kv: kv[1])[0])}")
            log("    stac only — objects will be mirrored bucket-to-bucket by seed")
        else:
            pq = download(pq_asset["href"], OUT / "parquet" / f"{cid}.parquet")
            download(mv_asset["href"], OUT / "pmtiles" / f"{cid}.pmtiles")
            columns = describe_columns(con, pq.as_posix())
            hex_columns = None
            if hex_asset:
                mirror_hex_prefix(cid, hex_asset["href"])
                parts = sorted((OUT / "hex" / cid).glob("h0=*/data_0.parquet"),
                               key=lambda f: f.stat().st_size)
                if parts:
                    hex_columns = describe_columns(con, parts[0].as_posix())

        write_stac(layer, source, columns, vector_layers, source_url, hex_columns)

    # ── Rasters ──────────────────────────────────────────────────────────────
    for raster in (RASTERS if not only else [r for r in RASTERS if r["id"] == only]):
        cid = raster["id"]
        # These sources sit at a bucket root rather than under SOURCE_BASE, so
        # the collection path is resolved against the host, not the census bucket.
        host = SOURCE_BASE.split("://", 1)[0] + "://" + SOURCE_BASE.split("://", 1)[1].split("/", 1)[0]
        source_url = f"{host}/{raster['source']}"
        log(f"\n==> {cid}")
        log(f"    source {source_url}")
        rsource = fetch_json(source_url)
        rasset = asset_href(rsource, raster["asset"], source_url)

        hex_assets = []
        if MIRROR_HEX:
            for h in raster.get("hexes", []):
                ha = asset_href(rsource, h["asset"], source_url)
                marker = f"/{h['sub']}/"
                base, _ = ha["href"].split(marker, 1)
                scheme, rest = base.split("://", 1)
                host2, bucket_and_path = rest.split("/", 1)
                bucket = bucket_and_path.split("/", 1)[0]
                bucket_url = f"{scheme}://{host2}/{bucket}"
                prefix = bucket_and_path.split("/", 1)[1] + marker
                objs = list_prefix(bucket_url, prefix.lstrip("/"))
                if not objs:
                    raise SystemExit(f"\n{cid}: no objects under {prefix}")
                total = sum(sz for _, sz in objs)
                dest_prefix = f"{h['sub']}/{cid}/"
                log(f"    {h['sub']}: {len(objs)} partition(s), {total / 1e9:.2f} GB")
                sample = f"{bucket_url}/{urllib.parse.quote(min(objs, key=lambda kv: kv[1])[0])}"
                if STAC_ONLY:
                    for key, sz in sorted(objs):
                        rel = key.split(marker, 1)[1]
                        manifest.append({"kind": "object", "size": sz,
                                         "src": f"{bucket_url}/{urllib.parse.quote(key)}",
                                         "dest": f"{dest_prefix}{rel}"})
                    cols = describe_columns(con, sample)
                else:
                    for key, sz in sorted(objs):
                        rel = key.split(marker, 1)[1]
                        download(f"{bucket_url}/{urllib.parse.quote(key)}",
                                 OUT / dest_prefix / rel)
                    parts = sorted((OUT / dest_prefix).glob("h0=*/data_0.parquet"),
                                   key=lambda f: f.stat().st_size)
                    cols = describe_columns(con, parts[0].as_posix())
                hex_assets.append({"key": h["sub"].replace("-", "_"),
                                   "dest_prefix": dest_prefix,
                                   "title": h["title"], "note": h["note"],
                                   "columns": cols})

        if STAC_ONLY:
            manifest.append({"kind": "object", "src": rasset["href"],
                             "dest": raster["dest"],
                             "size": remote_size(rasset["href"]) or 0})
            log("    stac only — objects will be mirrored bucket-to-bucket by seed")
        else:
            download(rasset["href"], OUT / raster["dest"])
        write_raster_stac(raster, rsource, rasset, hex_assets)

    write_catalog()
    write_provenance(layers)
    if STAC_ONLY:
        write_manifest(manifest)

    log("\n==> bundle complete")
    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    log(f"    {OUT}: {total / 1e9:.2f} GB")
    log("\nNext: make up && make seed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
