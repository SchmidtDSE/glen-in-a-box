#!/usr/bin/env bash
# Restore an air-gap bundle produced by airgap-save.sh. Runs offline.
set -euo pipefail

cd "$(dirname "$0")/.."

INDIR="${AIRGAP_DIR:-airgap}"

if [ ! -d "$INDIR" ]; then
    echo "No $INDIR/ directory found." >&2
    echo "Copy the archive directory produced by 'make airgap' here first." >&2
    exit 1
fi

cd "$INDIR"

# Work out which compression was used rather than assuming.
if   [ -f images.tar.zst ]; then EXT=zst; DECOMP="zstd -dc"
elif [ -f images.tar.gz  ]; then EXT=gz;  DECOMP="gzip -dc"
else
    echo "No images.tar.zst or images.tar.gz in $INDIR/" >&2
    exit 1
fi

if [ -f SHA256SUMS ]; then
    echo "==> verifying checksums"
    # Worth the minutes it costs: a truncated multi-gigabyte copy over a USB
    # stick or a flaky share is common, and every downstream symptom of one is
    # baffling — a corrupt image layer surfaces as an unrelated runtime error.
    sha256sum -c SHA256SUMS
else
    echo "warning: no SHA256SUMS in $INDIR/, skipping verification" >&2
fi

echo "==> loading images"
$DECOMP "images.tar.$EXT" | docker load

echo "==> restoring payload"
cd ..
$DECOMP "$INDIR/payload.tar.$EXT" | tar -x

[ -f MANIFEST.txt ] && cat "$INDIR/MANIFEST.txt" || true

cat <<'EOF'

==> loaded

Before starting, check PUBLIC_ORIGIN in .env matches how this machine will
actually be reached — localhost for a single user, a LAN IP for anything else.
Getting it wrong is the most common cause of "the map loads but no layers
appear", because the STAC hrefs are generated from it.

    make up
    make seed
    make verify
EOF
