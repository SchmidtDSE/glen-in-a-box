#!/usr/bin/env bash
# Pack the whole box into transferable archives.
#
# Two archives rather than one, because they have very different lifetimes:
# the image archive changes when you rebuild the app, the payload archive
# changes when you rebuild the data or swap the model. On a repeat delivery
# you usually only need to re-send one of them.
#
# Output lands in ./airgap/ and is gitignored. Total is typically 10-15 GB.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
[ -f .env ] && { set -a; . ./.env; set +a; }

OUTDIR="${AIRGAP_DIR:-airgap}"
mkdir -p "$OUTDIR"

# zstd is dramatically faster than gzip at this size and is present on most
# modern systems; fall back rather than fail if it is not.
if command -v zstd >/dev/null 2>&1; then
    COMP="zstd -T0 -3"; EXT="zst"
else
    echo "note: zstd not found, falling back to gzip (slower)" >&2
    COMP="gzip -1"; EXT="gz"
fi

echo "==> resolving image list from the compose file"
# Ask compose rather than hardcoding names, so this cannot drift out of sync
# with docker-compose.yml.
mapfile -t IMAGES < <(docker compose --profile bootstrap config --images | sort -u)
printf '    %s\n' "${IMAGES[@]}"

# A missing image here means `make images` was never run, or was run against a
# different TAG. Catch it now rather than half way through a 10 GB save.
for img in "${IMAGES[@]}"; do
    if ! docker image inspect "$img" >/dev/null 2>&1; then
        echo "" >&2
        echo "Image not present locally: $img" >&2
        echo "Run 'make images' first." >&2
        exit 1
    fi
done

echo "==> saving images → $OUTDIR/images.tar.$EXT"
docker save "${IMAGES[@]}" | $COMP > "$OUTDIR/images.tar.$EXT"

echo "==> saving payload → $OUTDIR/payload.tar.$EXT"
# basemap/config is included because build.sh generates the style JSONs; they
# are not in git and the basemap container will not start without them.
tar -c \
    data/bundle \
    llm/models \
    basemap/data \
    basemap/config \
    | $COMP > "$OUTDIR/payload.tar.$EXT"

echo "==> writing manifest"
{
    echo "glen-in-a-box air-gap bundle"
    echo "created:      $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "git revision: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
    echo "geo-agent:    ${GEO_AGENT_REF:-unknown}"
    echo "llm model:    ${LLM_MODEL_FILE:-unknown}"
    echo ""
    echo "images:"
    printf '  %s\n' "${IMAGES[@]}"
    echo ""
    echo "PUBLIC_ORIGIN at pack time: ${PUBLIC_ORIGIN:-unset}"
    echo "  If the target machine will be reached at a different address, set"
    echo "  PUBLIC_ORIGIN there and re-run 'make seed'. The data bundle keeps"
    echo "  its \${PUBLIC_ORIGIN} placeholders, so no rebuild is needed."
} > "$OUTDIR/MANIFEST.txt"

echo "==> checksums"
( cd "$OUTDIR" && sha256sum "images.tar.$EXT" "payload.tar.$EXT" > SHA256SUMS )

echo ""
echo "==> done"
du -sh "$OUTDIR"/*
cat <<EOF

Copy the whole $OUTDIR/ directory, plus a checkout of this repo, to the target
machine. There:

    make load
    make up
    make seed
EOF
