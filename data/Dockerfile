# Data mirror. Runs once, online, to copy the pre-processed census layers into
# the parquet + PMTiles + STAC bundle the offline box serves.
#
# Not part of the running stack — `make data` builds and runs it, and the output
# lands in ./data/bundle.
#
# ── Why this image is nearly empty ───────────────────────────────────────────
# It used to be a GDAL base plus tippecanoe compiled from source, because the
# bundle was built from scratch: TIGER shapefiles joined to ACS API responses in
# DuckDB, then tiled. All of that produced artifacts that already exist upstream
# as cloud-native assets, so mirror.py copies them instead. Gone with the old
# approach: the GDAL runtime, a from-source tippecanoe build, and the Census API
# key it needed.
#
# duckdb is here for one job — reading each downloaded parquet's schema so the
# STAC column docs describe the actual file rather than a hardcoded guess.

FROM python:3.12-slim

RUN pip install --no-cache-dir duckdb==1.1.3

WORKDIR /work
COPY mirror.py /work/mirror.py

# /out is the bundle destination, bind-mounted by `make data`. Downloads land
# there directly and are skipped on re-run when the size already matches, so
# there is no separate cache to keep.
VOLUME ["/out"]

ENTRYPOINT ["python3", "/work/mirror.py"]
