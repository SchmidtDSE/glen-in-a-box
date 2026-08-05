#!/usr/bin/env python3
"""Build the offline census data bundle.

Produces, for each of three geography levels:

    parquet/<id>.parquet     geometry + attributes, queried by DuckDB via MCP
    pmtiles/<id>.pmtiles     vector tiles, rendered by MapLibre in the browser
    stac/<id>/collection.json

plus a root ``stac/catalog.json`` tying them together.

ONLINE STEP. Run once with ``make data``; the output is what the offline box
actually serves.

Design notes worth knowing before editing
-----------------------------------------

*Parquet and PMTiles are both required, and they are not redundant.* The map
reads tiles; the agent's SQL reads parquet. Tiles carry only the handful of
attributes tippecanoe was told to keep, at simplified geometry — they cannot
answer an analytical question. Parquet carries full precision and every column
but cannot be rendered. Dropping either one silently removes half the app.

*The attribute set in the tiles is chosen, not inherited.* Every attribute in a
tile is paid for at every zoom level, in every tile, forever. So the tiles get
the few fields needed for styling and hover, and everything else lives in
parquet. This is also why ``tooltip_fields`` in layers-input.json must be
verified against the tiles rather than against the parquet schema — see
TILE_FIELDS below, which is the authoritative list.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import duckdb

OUT = Path(os.environ.get("OUT_DIR", "/out"))
CACHE = Path(os.environ.get("CACHE_DIR", "/cache"))

TIGER_YEAR = os.environ.get("TIGER_YEAR", "2024")
ACS_YEAR = os.environ.get("ACS_YEAR", "2023")
TIGER_BASE = f"https://www2.census.gov/geo/tiger/TIGER{TIGER_YEAR}"
ACS_BASE = f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5"

# Optional. The API serves anonymous requests fine at this volume (about 60
# calls); a key only matters if you are hammering it.
CENSUS_API_KEY = os.environ.get("CENSUS_API_KEY", "").strip()

# ACS variables. Kept deliberately short: each one costs a column in every
# parquet file and, if promoted to TILE_FIELDS, bytes in every tile.
ACS_VARS = {
    "B01003_001E": "pop_total",
    "B19013_001E": "median_income",
    "B17001_001E": "poverty_universe",
    "B17001_002E": "poverty_below",
    "B25077_001E": "median_home_value",
}

# The Census API encodes "no estimate" as large negative sentinels rather than
# null: -666666666 for suppressed, -999999999 and friends for other failures.
# Left alone these are catastrophic — a median income of -666666666 does not
# just look wrong, it silently wrecks every average, every choropleth break,
# and every "lowest income" ranking. Anything below this threshold becomes NULL.
SENTINEL_FLOOR = -1_000_000

# Attributes written into the vector tiles. Everything else stays in parquet.
TILE_FIELDS = {
    "states": ["GEOID", "NAME", "STUSPS", "pop_total", "median_income", "pop_density"],
    "counties": ["GEOID", "NAME", "STUSPS", "pop_total", "median_income", "pop_density", "poverty_rate"],
    "tracts": ["GEOID", "NAMELSAD", "pop_total", "median_income"],
}

# Tile zoom ranges, matched to how each layer is actually used. Tracts start at
# z5 because 85,000 of them at z0-4 is a vast amount of data for a view in
# which no individual tract is more than a pixel.
ZOOM = {
    "states": (0, 8),
    "counties": (0, 10),
    "tracts": (5, 12),
}

LEVELS = ["states", "counties", "tracts"]


# ─────────────────────────────────────────────────────────────────────────────
# Fetch helpers
# ─────────────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(msg, flush=True)


def download(url: str, dest: Path) -> Path:
    """Fetch to `dest`, skipping if already cached."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log(f"    GET {url}")
    try:
        with urllib.request.urlopen(url, timeout=300) as r, tmp.open("wb") as f:
            shutil.copyfileobj(r, f)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"\nHTTP {e.code} fetching {url}\n{e.reason}") from e
    # Rename only after a complete transfer, so an interrupted run does not
    # leave a truncated file that the cache check then treats as valid.
    tmp.rename(dest)
    return dest


def acs_get(params: dict[str, str]) -> list[list[str]]:
    """Query the ACS API, returning the raw [header, *rows] list."""
    q = dict(params)
    if CENSUS_API_KEY:
        q["key"] = CENSUS_API_KEY
    url = ACS_BASE + "?" + urllib.parse.urlencode(q, safe=":*")
    with urllib.request.urlopen(url, timeout=180) as r:
        return json.loads(r.read())


# ─────────────────────────────────────────────────────────────────────────────
# ACS
# ─────────────────────────────────────────────────────────────────────────────

def state_fips() -> list[str]:
    rows = acs_get({"get": "NAME", "for": "state:*"})
    # Skip the header row; the FIPS code is the trailing column.
    codes = sorted(r[-1] for r in rows[1:])
    # Territories other than Puerto Rico have patchy ACS coverage and no TIGER
    # tract files in the same layout, so they are excluded rather than left to
    # fail mid-run.
    return [c for c in codes if c not in {"60", "66", "69", "78"}]


def fetch_acs(level: str) -> Path:
    """Download ACS estimates for one geography level into a cached NDJSON."""
    dest = CACHE / "acs" / f"{level}-{ACS_YEAR}.json"
    if dest.exists() and dest.stat().st_size > 0:
        log(f"    cached: {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    variables = ",".join(ACS_VARS)
    records: list[dict] = []

    if level == "states":
        raw = [acs_get({"get": variables, "for": "state:*"})]
    elif level == "counties":
        raw = [acs_get({"get": variables, "for": "county:*", "in": "state:*"})]
    else:
        # Tracts must be requested one state at a time — the API refuses a
        # nationwide tract query.
        raw = []
        states = state_fips()
        for i, fips in enumerate(states, 1):
            log(f"    tracts {i}/{len(states)} (state {fips})")
            raw.append(acs_get({"get": variables, "for": "tract:*", "in": f"state:{fips} county:*"}))

    for table in raw:
        header, *rows = table
        idx = {name: i for i, name in enumerate(header)}
        for row in rows:
            # GEOID is the concatenation of the geography key columns, in
            # hierarchy order. This is the only reliable join key: county and
            # tract names are not unique across states.
            if level == "states":
                geoid = row[idx["state"]]
            elif level == "counties":
                geoid = row[idx["state"]] + row[idx["county"]]
            else:
                geoid = row[idx["state"]] + row[idx["county"]] + row[idx["tract"]]

            rec: dict[str, object] = {"GEOID": geoid}
            for var, col in ACS_VARS.items():
                raw_val = row[idx[var]]
                try:
                    val = float(raw_val) if raw_val not in (None, "", "null") else None
                except ValueError:
                    val = None
                if val is not None and val < SENTINEL_FLOOR:
                    val = None  # suppressed / unavailable sentinel
                rec[col] = val
            records.append(rec)

    with dest.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    log(f"    wrote {len(records):,} ACS rows → {dest.name}")
    return dest


# ─────────────────────────────────────────────────────────────────────────────
# TIGER
# ─────────────────────────────────────────────────────────────────────────────

def fetch_tiger(level: str) -> list[Path]:
    """Download TIGER/Line shapefiles, returning /vsizip-able archive paths."""
    d = CACHE / "tiger"
    if level == "states":
        return [download(f"{TIGER_BASE}/STATE/tl_{TIGER_YEAR}_us_state.zip",
                         d / f"tl_{TIGER_YEAR}_us_state.zip")]
    if level == "counties":
        return [download(f"{TIGER_BASE}/COUNTY/tl_{TIGER_YEAR}_us_county.zip",
                         d / f"tl_{TIGER_YEAR}_us_county.zip")]

    # Tracts ship one archive per state — there is no national file.
    out = []
    states = state_fips()
    for i, fips in enumerate(states, 1):
        log(f"    tract geometry {i}/{len(states)} (state {fips})")
        out.append(download(f"{TIGER_BASE}/TRACT/tl_{TIGER_YEAR}_{fips}_tract.zip",
                            d / f"tl_{TIGER_YEAR}_{fips}_tract.zip"))
    return out


def vsizip(path: Path) -> str:
    """GDAL virtual path to the .shp inside a TIGER zip."""
    return f"/vsizip/{path}/{path.stem}.shp"


# ─────────────────────────────────────────────────────────────────────────────
# Join and write
# ─────────────────────────────────────────────────────────────────────────────

def build_parquet(con: duckdb.DuckDBPyConnection, level: str,
                  shapefiles: list[Path], acs_path: Path) -> Path:
    """Join TIGER geometry to ACS estimates and write one GeoParquet file."""
    dest = OUT / "parquet" / f"us-census-{level}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)

    reads = " UNION ALL ".join(
        f"SELECT * FROM ST_Read('{vsizip(p)}')" for p in shapefiles
    )

    # STUSPS (the two-letter abbreviation) is only on the state file, so
    # counties and tracts get it by joining back on the leading two GEOID
    # digits. Doing this here means downstream SQL never has to know that.
    if level == "states":
        name_cols = "g.NAME, g.STUSPS"
        state_join = ""
    else:
        state_shp = vsizip(CACHE / "tiger" / f"tl_{TIGER_YEAR}_us_state.zip")
        name_cols = ("g.NAME, s.STUSPS"
                     if level == "counties"
                     else "g.NAMELSAD, g.NAME, s.STUSPS")
        state_join = (f"LEFT JOIN (SELECT GEOID AS sg, STUSPS FROM ST_Read('{state_shp}')) s "
                      f"ON s.sg = substr(g.GEOID, 1, 2)")

    con.execute(f"""
        COPY (
            SELECT
                g.GEOID,
                {name_cols},
                a.pop_total,
                a.median_income,
                a.median_home_value,
                a.poverty_universe,
                a.poverty_below,

                -- Derived measures, computed once here so that neither the
                -- agent's SQL nor the map style has to get them right.
                -- ALAND is in square metres; 2 589 988 m² to the square mile.
                CASE WHEN g.ALAND > 0
                     THEN a.pop_total / (g.ALAND / 2589988.0)
                END AS pop_density,

                -- Guard the denominator: poverty_universe is 0 for unpopulated
                -- tracts, and 0/0 would produce NaN, which parquet carries
                -- happily and every consumer then mishandles differently.
                CASE WHEN a.poverty_universe > 0
                     THEN 100.0 * a.poverty_below / a.poverty_universe
                END AS poverty_rate,

                g.ALAND  AS land_area_m2,
                g.AWATER AS water_area_m2,
                g.geom   AS geometry
            FROM ({reads}) g
            LEFT JOIN read_json_auto('{acs_path}') a ON a.GEOID = g.GEOID
            {state_join}
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD);
    """)

    n = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
    unmatched = con.execute(
        f"SELECT count(*) FROM read_parquet('{dest}') WHERE pop_total IS NULL"
    ).fetchone()[0]
    log(f"    {dest.name}: {n:,} rows ({unmatched:,} without ACS match)")

    # A large unmatched fraction means the GEOID join failed — usually a
    # TIGER/ACS vintage mismatch. Worth shouting about, because the result
    # still looks like a valid file.
    if n and unmatched / n > 0.05:
        log(f"    WARNING: {unmatched / n:.1%} of {level} have no ACS match. "
            f"Check that TIGER_YEAR={TIGER_YEAR} and ACS_YEAR={ACS_YEAR} are compatible.")
    return dest


def build_pmtiles(con: duckdb.DuckDBPyConnection, level: str, parquet: Path) -> Path:
    """Render one parquet file to a PMTiles archive via tippecanoe."""
    dest = OUT / "pmtiles" / f"us-census-{level}.pmtiles"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)

    # tippecanoe reads GeoJSON, so stream the tile attributes out as
    # newline-delimited features. Only TILE_FIELDS are included — see the
    # module docstring on why the tile schema is curated rather than inherited.
    geojson = CACHE / "geojsonseq" / f"{level}.geojsonl"
    geojson.parent.mkdir(parents=True, exist_ok=True)
    fields = ", ".join(TILE_FIELDS[level])
    con.execute(f"""
        COPY (SELECT {fields}, geometry FROM read_parquet('{parquet}'))
        TO '{geojson}' (FORMAT GDAL, DRIVER 'GeoJSONSeq');
    """)

    minzoom, maxzoom = ZOOM[level]
    layer = f"us-census-{level}"

    cmd = [
        "tippecanoe",
        "-o", str(dest),
        "-l", layer,                    # must match vector:layers in the STAC asset
        "-Z", str(minzoom),
        "-z", str(maxzoom),
        # Keep every feature. The defaults drop features to hit a tile size
        # budget, which for census geography means whole counties silently
        # vanishing at low zoom — worse than a slightly larger tile.
        "--drop-densest-as-needed",
        "--no-feature-limit",
        "--no-tile-size-limit",
        # Simplification is what keeps this from being enormous. Retaining
        # boundaries at low zoom matters more than their exact shape.
        "--simplification=4",
        "--detect-shared-borders",
        "--coalesce-densest-as-needed",
        "--force",
        str(geojson),
    ]
    log(f"    tippecanoe → {dest.name} (z{minzoom}-{maxzoom})")
    subprocess.run(cmd, check=True)
    log(f"    {dest.name}: {dest.stat().st_size / 1e6:.0f} MB")
    return dest


# ─────────────────────────────────────────────────────────────────────────────
# STAC
# ─────────────────────────────────────────────────────────────────────────────

# Hrefs are emitted with a ${PUBLIC_ORIGIN} placeholder rather than a hostname.
# The seed job substitutes it at upload time, which is what lets one bundle
# serve localhost, a LAN address, or a reverse proxy without a rebuild.
HREF = "${PUBLIC_ORIGIN}/data/geo-data"

TITLES = {
    "states": "US States",
    "counties": "US Counties",
    "tracts": "US Census Tracts",
}

DESCRIPTIONS = {
    "states": f"State boundaries from TIGER/Line {TIGER_YEAR}, joined to "
              f"American Community Survey {ACS_YEAR} 5-year estimates.",
    "counties": f"County and county-equivalent boundaries from TIGER/Line "
                f"{TIGER_YEAR}, joined to ACS {ACS_YEAR} 5-year estimates.",
    "tracts": f"Census tract boundaries from TIGER/Line {TIGER_YEAR}, joined to "
              f"ACS {ACS_YEAR} 5-year estimates. Tract-level estimates carry "
              f"wide margins of error; treat small differences as noise.",
}

COLUMN_DOCS = [
    ("GEOID", "string", "Census geographic identifier. The join key — names are not unique."),
    ("NAME", "string", "Geography name."),
    ("STUSPS", "string", "Two-letter state abbreviation."),
    ("pop_total", "double", "Total population (ACS B01003_001E)."),
    ("median_income", "double", "Median household income in dollars (ACS B19013_001E). "
                                "NULL where suppressed — never zero."),
    ("median_home_value", "double", "Median owner-occupied home value (ACS B25077_001E)."),
    ("poverty_universe", "double", "Population for whom poverty status is determined (B17001_001E)."),
    ("poverty_below", "double", "Population below the poverty line (B17001_002E)."),
    ("pop_density", "double", "People per square mile, derived from pop_total and land area."),
    ("poverty_rate", "double", "Percent below the poverty line, derived. NULL where the universe is 0."),
    ("land_area_m2", "double", "Land area in square metres (TIGER ALAND)."),
    ("water_area_m2", "double", "Water area in square metres (TIGER AWATER)."),
    ("geometry", "geometry", "Polygon boundary, EPSG:4326."),
]


def write_stac(level: str) -> None:
    cid = f"us-census-{level}"
    d = OUT / "stac" / cid
    d.mkdir(parents=True, exist_ok=True)

    # NAMELSAD only exists on the tract file; STUSPS is absent from the raw
    # state file's perspective but present in all three outputs.
    columns = [
        {"name": n, "type": t, "description": desc}
        for n, t, desc in COLUMN_DOCS
        if not (n == "NAME" and level == "tracts")
    ]
    if level == "tracts":
        columns.insert(1, {"name": "NAMELSAD", "type": "string",
                           "description": "Tract name including the 'Census Tract' prefix."})

    collection = {
        "type": "Collection",
        "stac_version": "1.0.0",
        "stac_extensions": [
            "https://stac-extensions.github.io/table/v1.2.0/schema.json",
        ],
        "id": cid,
        "title": TITLES[level],
        "description": DESCRIPTIONS[level],
        "license": "CC0-1.0",
        "providers": [
            {"name": "US Census Bureau", "roles": ["producer", "licensor"],
             "url": "https://www.census.gov/"},
        ],
        "extent": {
            # Continental US plus Alaska and Hawaii. Alaska crosses the
            # antimeridian, hence -180 rather than the mainland's ~-125.
            "spatial": {"bbox": [[-180.0, 15.0, -64.0, 72.0]]},
            "temporal": {"interval": [[f"{ACS_YEAR}-01-01T00:00:00Z",
                                       f"{ACS_YEAR}-12-31T23:59:59Z"]]},
        },
        "links": [
            {"rel": "root", "href": f"{HREF}/stac/catalog.json", "type": "application/json"},
            {"rel": "parent", "href": f"{HREF}/stac/catalog.json", "type": "application/json"},
            {"rel": "self", "href": f"{HREF}/stac/{cid}/collection.json", "type": "application/json"},
        ],
        "table:columns": columns,
        "assets": {
            "pmtiles": {
                "href": f"{HREF}/pmtiles/{cid}.pmtiles",
                "type": "application/vnd.pmtiles",
                "title": f"{TITLES[level]} vector tiles",
                "roles": ["visual", "tiles"],
                # Without this the app falls back to using the asset key
                # ("pmtiles") as the source-layer name, finds no such layer in
                # the archive, and renders nothing at all — with no error.
                # It must equal tippecanoe's -l argument.
                "vector:layers": [cid],
            },
            "parquet": {
                "href": f"{HREF}/parquet/{cid}.parquet",
                "type": "application/vnd.apache.parquet",
                "title": f"{TITLES[level]} table",
                "roles": ["data"],
                "table:columns": columns,
            },
        },
    }
    (d / "collection.json").write_text(json.dumps(collection, indent=2))
    log(f"    stac/{cid}/collection.json")


def write_catalog() -> None:
    catalog = {
        "type": "Catalog",
        "stac_version": "1.0.0",
        "id": "glen-in-a-box",
        "title": "Glen in a Box — offline US census catalog",
        "description": (
            "US Census Bureau geography and American Community Survey estimates, "
            "bundled for fully offline use. Every asset is served from the local "
            "object store; nothing here resolves to the internet."
        ),
        "links": [
            {"rel": "root", "href": f"{HREF}/stac/catalog.json", "type": "application/json"},
            {"rel": "self", "href": f"{HREF}/stac/catalog.json", "type": "application/json"},
            *[
                {
                    "rel": "child",
                    "href": f"{HREF}/stac/us-census-{lvl}/collection.json",
                    "type": "application/json",
                    "title": TITLES[lvl],
                }
                for lvl in LEVELS
            ],
        ],
    }
    (OUT / "stac").mkdir(parents=True, exist_ok=True)
    (OUT / "stac" / "catalog.json").write_text(json.dumps(catalog, indent=2))
    log("    stac/catalog.json")


# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute(f"SET threads TO {os.environ.get('BUILD_THREADS', '4')};")
    con.execute(f"SET memory_limit = '{os.environ.get('BUILD_MEMORY', '4GB')}';")

    only = os.environ.get("ONLY_LEVEL", "").strip()
    levels = [only] if only else LEVELS
    if only and only not in LEVELS:
        raise SystemExit(f"ONLY_LEVEL must be one of {LEVELS}, got {only!r}")

    # The state shapefile is needed even when building only counties or
    # tracts, because that is where STUSPS comes from.
    fetch_tiger("states")

    for level in levels:
        log(f"\n==> {level}")
        log("  fetching ACS")
        acs = fetch_acs(level)
        log("  fetching TIGER geometry")
        shp = fetch_tiger(level)
        log("  joining → parquet")
        pq = build_parquet(con, level, shp, acs)
        log("  building tiles")
        build_pmtiles(con, level, pq)
        log("  writing STAC")
        write_stac(level)

    write_catalog()

    log("\n==> bundle complete")
    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    log(f"    {OUT}: {total / 1e9:.2f} GB")
    log("\nNext: make up && make seed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
