#!/bin/sh
# Create the buckets, apply their access policies, and upload ./data/bundle.
#
# Idempotent: safe to re-run after rebuilding the data bundle or after changing
# PUBLIC_ORIGIN (which you MUST do, since the origin is baked into STAC hrefs).
#
# Reads only local files. No network beyond the compose bridge.
set -eu

: "${S3_ENDPOINT:?S3_ENDPOINT is required}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY is required}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY is required}"
: "${PUBLIC_ORIGIN:?PUBLIC_ORIGIN is required}"

BUNDLE=/bundle
ALIAS=glen

# The STAC substitution below is the one step here that can fail WITHOUT
# failing: `for f in $(find ...)` with no find is an empty loop, and `if grep
# -q` with no grep is just a false branch. Neither trips `set -e`, so the
# result was a seed that exited 0 after uploading unrendered hrefs. Assert the
# userland up front instead — see seed/Dockerfile.
for tool in rc find grep sed mktemp; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "seed: required tool '$tool' is not in this image." >&2
        echo "      Add it to seed/Dockerfile — do not let it fail silently." >&2
        exit 1
    }
done

if [ ! -d "$BUNDLE" ] || [ -z "$(ls -A "$BUNDLE" 2>/dev/null)" ]; then
    cat >&2 <<EOF

The data bundle at ./data/bundle is missing or empty.

It is not committed to git — it is roughly 1-2 GB of derived parquet and
PMTiles. Build it once, with a network connection:

    make data

or unpack a prebuilt bundle:

    tar -xzf glen-data-bundle.tar.gz -C data/

EOF
    exit 1
fi

# rc stores its alias config under $HOME by default; the image runs as an
# unprivileged uid whose home may not be writable, so point it somewhere that
# always is. Nothing here needs to persist beyond this run.
export RC_CONFIG_DIR="${RC_CONFIG_DIR:-/tmp/rc}"

echo "==> connecting to $S3_ENDPOINT"
rc alias set "$ALIAS" "$S3_ENDPOINT" "$S3_ACCESS_KEY" "$S3_SECRET_KEY" >/dev/null

# ── Buckets ──────────────────────────────────────────────────────────────────
# geo-data is anonymously readable and tiles is not, and that asymmetry is
# forced by mcp-data-server: its DEFAULT DuckDB S3 secret is anonymous, so any
# bucket it reads without a scoped secret must allow anonymous GET. The tiles
# bucket gets a credentialed scoped secret instead (see S3_SOURCES in
# docker-compose.yml) because register_hex_tiles has to WRITE to it, and an
# anonymously writable bucket on a LAN-visible box is a bad trade.
echo "==> creating buckets"
rc bucket create --ignore-existing "$ALIAS/geo-data"
rc bucket create --ignore-existing "$ALIAS/tiles"

# rc spells mc's `none` as `private`. Allowed values are private, public,
# download and upload — an invalid one is a hard error, not a warning.
echo "==> setting policies"
rc bucket anonymous set download "$ALIAS/geo-data"
rc bucket anonymous set private  "$ALIAS/tiles"

# ── STAC templating ──────────────────────────────────────────────────────────
# The catalog ships with ${PUBLIC_ORIGIN} placeholders rather than a baked
# hostname, because the same bundle has to work on localhost, on a LAN IP, and
# behind someone else's proxy. Substitution happens here, at seed time, so the
# hrefs the browser follows and the hrefs the MCP server rewrites to s3:// are
# generated from one value.
#
# sed rather than envsubst: the base image has no gettext, and one substitution
# does not justify another package.
echo "==> rendering STAC catalog for PUBLIC_ORIGIN=$PUBLIC_ORIGIN"
STAGE=$(mktemp -d)
cp -r "$BUNDLE/." "$STAGE/"

rendered=0
while IFS= read -r f; do
    if grep -q '${PUBLIC_ORIGIN}' "$f"; then
        sed -i "s|\${PUBLIC_ORIGIN}|$PUBLIC_ORIGIN|g" "$f"
        rendered=$((rendered + 1))
    fi
done <<EOF
$(find "$STAGE" -name '*.json')
EOF
echo "    rendered $rendered STAC document(s)"

# Every bundle data/build.py produces emits ${PUBLIC_ORIGIN} placeholders, so
# finding none means something is wrong — a stale bundle with hardcoded hrefs,
# or a bundle whose JSON did not arrive. Uploading it anyway produces a catalog
# the browser cannot follow and an agent that reports having no datasets, with
# no error anywhere. Refuse instead.
if [ "$rendered" -eq 0 ]; then
    echo "" >&2
    echo "seed: no \${PUBLIC_ORIGIN} placeholders found in any bundle JSON." >&2
    echo "      Nothing was uploaded. Expected hrefs like" >&2
    echo "        \"\${PUBLIC_ORIGIN}/data/geo-data/pmtiles/...\"" >&2
    echo "      in $BUNDLE/stac/*/collection.json." >&2
    echo "      Rebuild the bundle with: make data" >&2
    exit 1
fi

# Belt and braces: prove the substitution actually landed before uploading.
if grep -rq '${PUBLIC_ORIGIN}' "$STAGE"; then
    echo "seed: placeholders survived substitution — refusing to upload." >&2
    grep -rl '${PUBLIC_ORIGIN}' "$STAGE" >&2
    exit 1
fi

# ── Upload ───────────────────────────────────────────────────────────────────
echo "==> uploading bundle to s3://geo-data"
rc mirror --overwrite --remove "$STAGE" "$ALIAS/geo-data"

rm -rf "$STAGE"

echo ""
echo "==> seed complete"
# `rc du` needs a rustfs data-usage route this server does not advertise, so
# count objects from the listing instead of asking for a summary.
echo "    $(rc object list --recursive "$ALIAS/geo-data" | wc -l) object(s) in s3://geo-data"
echo ""
echo "Catalog should now be readable at:"
echo "  $PUBLIC_ORIGIN/data/geo-data/stac/catalog.json"
