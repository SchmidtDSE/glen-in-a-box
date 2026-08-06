# One-shot bucket seeder. Runs under the `bootstrap` compose profile only.
#
# ── Why rc and not MinIO's mc ────────────────────────────────────────────────
# `rc` is rustfs's own S3 client: MIT/Apache-2.0, a static Rust binary, and the
# natural pairing with the rustfs server this stack already runs. MinIO's mc is
# AGPL and MinIO has been steadily withdrawing from open source, which is not a
# dependency worth carrying in a box meant to keep working untouched for years.
#
# It also happens to fix a bug. The minio/mc image is built on ubi-micro and
# ships mc plus a shell and almost nothing else — no find, no grep, no sed.
# entrypoint.sh needs all three to substitute ${PUBLIC_ORIGIN} into the STAC
# hrefs, and their absence did NOT fail loudly: `find` reporting "command not
# found" inside `for f in $(find ...)` yields an empty loop, so the substitution
# silently did nothing, `set -e` never tripped, and seed exited 0 having
# uploaded a catalog full of literal ${PUBLIC_ORIGIN} strings. The app then
# fetches a catalog whose every href is unresolvable and the agent reports
# having no datasets.
#
# rustfs/rc is Alpine-based and carries a normal busybox userland, so the tools
# are simply there. entrypoint.sh asserts them anyway — see its header.
#
# Pinned, not :latest, so a rebuild years from now produces the same seeder.
FROM rustfs/rc:v0.1.31

COPY entrypoint.sh /entrypoint.sh

# The base image's entrypoint is `rc` itself; we need a shell script instead.
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
