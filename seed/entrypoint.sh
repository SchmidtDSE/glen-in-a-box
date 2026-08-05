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

echo "==> connecting to $S3_ENDPOINT"
mc alias set "$ALIAS" "$S3_ENDPOINT" "$S3_ACCESS_KEY" "$S3_SECRET_KEY" >/dev/null

# ── Buckets ──────────────────────────────────────────────────────────────────
# geo-data is anonymously readable and tiles is not, and that asymmetry is
# forced by mcp-data-server: its DEFAULT DuckDB S3 secret is anonymous, so any
# bucket it reads without a scoped secret must allow anonymous GET. The tiles
# bucket gets a credentialed scoped secret instead (see S3_SOURCES in
# docker-compose.yml) because register_hex_tiles has to WRITE to it, and an
# anonymously writable bucket on a LAN-visible box is a bad trade.
echo "==> creating buckets"
mc mb --ignore-existing "$ALIAS/geo-data"
mc mb --ignore-existing "$ALIAS/tiles"

echo "==> setting policies"
mc anonymous set download "$ALIAS/geo-data"
mc anonymous set none     "$ALIAS/tiles"

# ── STAC templating ──────────────────────────────────────────────────────────
# The catalog ships with ${PUBLIC_ORIGIN} placeholders rather than a baked
# hostname, because the same bundle has to work on localhost, on a LAN IP, and
# behind someone else's proxy. Substitution happens here, at seed time, so the
# hrefs the browser follows and the hrefs the MCP server rewrites to s3:// are
# generated from one value.
#
# sed rather than envsubst: the mc base image has no gettext, and one
# substitution does not justify another package.
echo "==> rendering STAC catalog for PUBLIC_ORIGIN=$PUBLIC_ORIGIN"
STAGE=$(mktemp -d)
cp -r "$BUNDLE/." "$STAGE/"

rendered=0
for f in $(find "$STAGE" -name '*.json'); do
    if grep -q '${PUBLIC_ORIGIN}' "$f" 2>/dev/null; then
        sed -i "s|\${PUBLIC_ORIGIN}|$PUBLIC_ORIGIN|g" "$f"
        rendered=$((rendered + 1))
    fi
done
echo "    rendered $rendered STAC document(s)"

if [ "$rendered" -eq 0 ]; then
    echo "    warning: no \${PUBLIC_ORIGIN} placeholders found." >&2
    echo "    If this bundle was built by an older make data, its hrefs may be" >&2
    echo "    hardcoded to a different origin and layers will fail to load." >&2
fi

# ── Upload ───────────────────────────────────────────────────────────────────
echo "==> uploading bundle to s3://geo-data"
mc mirror --overwrite --remove "$STAGE" "$ALIAS/geo-data"

rm -rf "$STAGE"

echo ""
echo "==> seed complete"
mc ls --recursive --summarize "$ALIAS/geo-data" | tail -3
echo ""
echo "Catalog should now be readable at:"
echo "  $PUBLIC_ORIGIN/data/geo-data/stac/catalog.json"
