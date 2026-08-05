# Data builder. Runs once, online, to turn TIGER/Line geometry and ACS tables
# into the parquet + PMTiles + STAC bundle the offline box serves.
#
# Not part of the running stack — `make data` builds and runs it, and the
# output lands in ./data/bundle. Keeping it separate means the runtime images
# carry no GDAL, no tippecanoe, and no build toolchain.

FROM ghcr.io/osgeo/gdal:ubuntu-small-3.9.2

ENV DEBIAN_FRONTEND=noninteractive

# tippecanoe has no Ubuntu package, so it is built from source. The -dev
# packages are its build inputs; they stay in the image because this is a
# throwaway builder, not something shipped to the field.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        curl \
        ca-certificates \
        libsqlite3-dev \
        zlib1g-dev \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

ARG TIPPECANOE_VERSION=2.78.0
RUN git clone --depth 1 --branch "${TIPPECANOE_VERSION}" \
        https://github.com/felt/tippecanoe.git /tmp/tippecanoe \
    && make -C /tmp/tippecanoe -j"$(nproc)" \
    && make -C /tmp/tippecanoe install \
    && rm -rf /tmp/tippecanoe

# --break-system-packages: Ubuntu 24.04 marks the system Python as externally
# managed. A venv would be tidier, but this image exists to run one script.
RUN pip3 install --no-cache-dir --break-system-packages duckdb==1.1.3

WORKDIR /work
COPY build.py /work/build.py

# /out is the bundle destination, /cache holds downloaded TIGER and ACS
# responses so a re-run after a failed tile build does not re-download 56
# shapefiles. Both are bind-mounted by `make data`.
VOLUME ["/out", "/cache"]

ENTRYPOINT ["python3", "/work/build.py"]
