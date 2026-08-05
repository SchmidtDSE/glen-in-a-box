#!/bin/sh
# Snapshot third-party browser dependencies listed in manifest.tsv.
#
# Runs ONCE, inside the webapp image build, which is the only online phase.
# Every byte is verified against a pinned sha384 as it arrives; after this the
# bundle is sealed and nothing ever re-fetches.
#
# Usage: fetch.sh <manifest.tsv> <outdir>
set -eu

MANIFEST="${1:?usage: fetch.sh <manifest.tsv> <outdir>}"
OUTDIR="${2:?usage: fetch.sh <manifest.tsv> <outdir>}"

fail=0
count=0

# Field-split on tab only, so URLs and digests keep any stray spaces intact.
OLDIFS="$IFS"
IFS='	'

while read -r dest sha url || [ -n "${dest:-}" ]; do
    # Skip comments and blank lines.
    case "$dest" in ''|'#'*) continue ;; esac
    [ -n "${url:-}" ] || { echo "malformed manifest row: $dest" >&2; fail=1; continue; }

    target="$OUTDIR/$dest"
    mkdir -p "$(dirname "$target")"

    printf '  fetch %-40s ' "$dest"
    if ! curl -fsSL --retry 3 --retry-delay 2 --max-time 120 -o "$target" "$url"; then
        echo "FAILED (download)"
        fail=1
        continue
    fi

    if [ "$sha" = "-" ]; then
        echo "ok (unverified)"
    else
        got="sha384-$(openssl dgst -sha384 -binary "$target" | openssl base64 -A)"
        if [ "$got" != "$sha" ]; then
            echo "FAILED (integrity)"
            # Print both so a legitimate version bump is a one-line copy-paste,
            # while a compromised or truncated download is unmistakable.
            echo "      expected: $sha" >&2
            echo "      actual:   $got" >&2
            echo "      url:      $url" >&2
            fail=1
            continue
        fi
        echo "ok (verified)"
    fi
    count=$((count + 1))
done < "$MANIFEST"

IFS="$OLDIFS"

if [ "$fail" -ne 0 ]; then
    echo "" >&2
    echo "vendor/fetch.sh: one or more dependencies failed." >&2
    echo "Do NOT work around this by setting the digest to '-'. Either the" >&2
    echo "upstream file changed (update the URL and digest together) or the" >&2
    echo "download was tampered with or truncated." >&2
    exit 1
fi

echo "vendor/fetch.sh: $count file(s) snapshotted to $OUTDIR"
