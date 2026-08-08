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

# Keep the files that record upstream URLs out of the served bucket. Neither is
# reachable from the catalog, so neither reaches the agent — but the bucket is
# what an offline box serves, and an external URL has no business in it. They
# stay on disk in data/bundle for whoever needs to know what to re-mirror.
rm -f "$STAGE"/HYDRATE-MANIFEST.* "$STAGE/stac/MIRROR-PROVENANCE.json"

# The data directories are handled object-by-object by the hydration step below,
# which skips what is already in place. Leaving them here too would re-upload
# every byte on every seed.
rm -rf "$STAGE/parquet" "$STAGE/pmtiles" "$STAGE/hex"

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

# Every bundle data/mirror.py produces emits ${PUBLIC_ORIGIN} placeholders, so
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

# ── Hydrate the data objects ─────────────────────────────────────────────────
# The parquet/pmtiles/hex objects are byte-identical copies of an upstream
# catalog, so they are streamed straight into this bucket rather than staged on
# local disk first — 15 GB written once instead of downloaded, stored, and
# uploaded again. data/mirror.py writes the plan; the source URLs live only in
# that file and never in the STAC, because anything reachable from the catalog
# ends up in the agent's context.
#
# Objects already present with the right size are skipped, which is what makes
# this safe to re-run AND what makes an air-gapped restore work: with the rustfs
# volume restored from a payload, every object is already there, nothing is
# fetched, and no network is touched.
MANIFEST="$BUNDLE/HYDRATE-MANIFEST.tsv"
if [ -f "$MANIFEST" ]; then
    echo "==> hydrating data objects into s3://geo-data"
    have=0; got=0; missing=0
    OLDIFS="$IFS"; IFS="$(printf '\t')"
    while read -r dest size src || [ -n "${dest:-}" ]; do
        case "$dest" in ''|'#'*) continue ;; esac
        [ -n "${src:-}" ] || continue

        actual=$(rc object stat --json "$ALIAS/geo-data/$dest" 2>/dev/null \
                 | sed -n 's/.*"size_bytes"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p')
        if [ -n "$actual" ] && [ "$actual" = "$size" ]; then
            have=$((have + 1))
            continue
        fi

        printf '    %-58s %s MB ' "$dest" "$((size / 1000000))"

        # Prefer a local copy when there is one. That covers two cases with one
        # branch: a bundle materialised with STAC_ONLY=0, and an air-gapped
        # machine where the files travelled on disk. Only fall back to the
        # network when neither the bucket nor the disk already has the object.
        if [ -f "$BUNDLE/$dest" ]; then
            if rc cp "$BUNDLE/$dest" "$ALIAS/geo-data/$dest" >/dev/null 2>&1; then
                echo "from disk"
                got=$((got + 1))
            else
                echo "FAILED (local copy)"
                missing=$((missing + 1))
            fi
        elif wget -q -O - "$src" | rc pipe "$ALIAS/geo-data/$dest" >/dev/null 2>&1; then
            echo "copied"
            got=$((got + 1))
        else
            echo "FAILED"
            missing=$((missing + 1))
        fi
    done <<EOF
$(cat "$MANIFEST")
EOF
    IFS="$OLDIFS"
    echo "    $have already present, $got copied, $missing failed"

    # ── Prune ────────────────────────────────────────────────────────────────
    # An object under a manifest-owned prefix that the manifest no longer lists is
    # stale — a layer or asset that was dropped. Without this it lingers forever,
    # invisible and costing disk, which is exactly what happened when the tract
    # hex was removed and 6.7 GB stayed behind.
    #
    # Scoped to the prefixes this manifest owns, so nothing else in the bucket is
    # at risk: the STAC is uploaded separately below, and register_hex_tiles
    # writes to the `tiles` bucket, not here.
    echo "==> pruning objects the manifest no longer lists"
    WANT=$(mktemp); STALE=$(mktemp)
    sed -n 's/^\([^#][^\t]*\)\t.*/\1/p' "$MANIFEST" | sort > "$WANT"
    # `rc object list --recursive` prints the key from the bucket root, so the
    # last field is directly comparable to a manifest dest.
    for prefix in parquet/ pmtiles/ hex/ hex-fractions/ cog/; do
        rc object list --recursive "$ALIAS/geo-data/$prefix" 2>/dev/null | awk '{print $NF}'
    done | sort -u | grep -vxF -f "$WANT" > "$STALE" || true

    # These two are excluded from the STAC upload because they carry upstream
    # URLs, but the STAC mirror runs without --remove, so a copy left by an
    # earlier seed would sit in the bucket forever. Remove them by name; the
    # prune below cannot, since it only knows the data prefixes.
    for leftover in stac/MIRROR-PROVENANCE.json HYDRATE-MANIFEST.json HYDRATE-MANIFEST.tsv; do
        rc object stat "$ALIAS/geo-data/$leftover" >/dev/null 2>&1 && {
            echo "    - $leftover (upstream URLs do not belong in the bucket)"
            rc object remove "$ALIAS/geo-data/$leftover" >/dev/null 2>&1
        }
    done

    pruned=0
    while read -r stale; do
        [ -n "$stale" ] || continue
        echo "    - $stale"
        rc object remove "$ALIAS/geo-data/$stale" >/dev/null 2>&1 && pruned=$((pruned + 1))
    done < "$STALE"
    echo "    $pruned object(s) removed"
    rm -f "$WANT" "$STALE"

    if [ "$missing" -ne 0 ]; then
        echo "" >&2
        echo "seed: $missing object(s) could not be fetched." >&2
        echo "      If this machine is offline, the payload it was restored from" >&2
        echo "      did not include them — a bundle built with STAC_ONLY=1 keeps" >&2
        echo "      the objects in the rustfs volume, so that volume has to travel" >&2
        echo "      with it. See scripts/airgap-save.sh." >&2
        exit 1
    fi
fi

# ── Upload the STAC ──────────────────────────────────────────────────────────
# --remove is deliberately NOT used: the data objects hydrated above are not in
# $STAGE, and mirroring with --remove would delete every one of them.
echo "==> uploading STAC to s3://geo-data"
rc mirror --overwrite "$STAGE" "$ALIAS/geo-data"

rm -rf "$STAGE"

echo ""
echo "==> seed complete"
# `rc du` needs a rustfs data-usage route this server does not advertise, so
# count objects from the listing instead of asking for a summary.
echo "    $(rc object list --recursive "$ALIAS/geo-data" | wc -l) object(s) in s3://geo-data"
echo ""
echo "Catalog should now be readable at:"
echo "  $PUBLIC_ORIGIN/data/geo-data/stac/catalog.json"
