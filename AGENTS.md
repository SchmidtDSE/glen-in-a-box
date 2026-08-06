# AI Agent Guide — glen-in-a-box

An offline geo-agent deployment. Read this before changing anything; several
files look editable but are load-bearing in non-obvious ways.

## The one rule

**Build is online. Run is offline.** Anything that touches the network must
happen in `make bundle` — never at container start, never at request time.

Before adding any URL to any file, ask which phase it runs in. If the answer is
"run", it does not belong. Vendor it in
[`app/vendor/manifest.tsv`](app/vendor/manifest.tsv) instead.

## Repo relationships

| Repo | Role |
|---|---|
| [geo-agent](https://github.com/boettiger-lab/geo-agent) | Core library. Snapshotted into the webapp image at `GEO_AGENT_REF`, then patched. Never modified here. |
| [mcp-data-server](https://github.com/boettiger-lab/mcp-data-server) | SQL + STAC backend. Used as a published image (pin `MCP_TAG` to a version tag — `latest` has shipped broken), configured by env only. |
| [geo-agent-template](https://github.com/boettiger-lab/geo-agent-template) | The online template this is the offline sibling of. |

## What to edit, and what not to

**Edit freely:** `app/layers-input.json`, `app/system-prompt.md`,
`data/mirror.py`, the docs.

**`app/layers-input.json` is strict JSON** — no comments. Prose goes in the
`_comment` array that is already there.

**Edit carefully:** `app/vendor/*` — this is the offline machinery. See below.

**Do not write JavaScript.** The frontend is a snapshot of geo-agent. Behaviour
changes belong upstream. The only exception is
[`patch-geoagent.mjs`](app/vendor/patch-geoagent.mjs), and only to redirect a
URL — never to change logic.

**Do not commit build artifacts:** `data/bundle/`, `llm/models/`,
`basemap/data/`, `basemap/config/styles/`, `airgap/`. All gitignored, all large.

## Things that fail silently

Every one of these produces a working-looking system that is subtly broken.
They are the reason this repo is stricter than it might appear to need to be.

**Lazy CDN loads.** d3, Observable Plot, and mapbox-gl-draw load from inside
geo-agent modules the *first time a feature is used*, not at page load. An
offline bundle that misses them looks perfect until someone asks for a chart.
If you bump `GEO_AGENT_REF`, check for new ones — the patcher's
"UNREVIEWED external URLs" sweep is there for this.

**Missing `vector:layers`.** A STAC `pmtiles` asset without it makes the app
use the asset key as the source-layer name. MapLibre finds no such layer and
renders nothing, with no error. It must equal tippecanoe's `-l` argument.

**`PUBLIC_ORIGIN` drift.** `layers-input.json` is relative and always loads;
the STAC catalog is absolute and baked at seed time. A mismatch gives you a map
with no layers and no error. Re-run `make seed` after changing it.

**Tile fields versus parquet fields.** A PMTiles archive carries fewer columns
than its parquet sibling, and the mirrored tiles use UPSTREAM column names
(`MHHI`, not `median_income`). A `tooltip_fields` or style entry naming a
parquet-only or renamed column renders blank with no error. Check against
`pmtiles show --metadata`, not against the parquet schema.

**Missing `--jinja`.** Without it llama.cpp emits no `tool_calls` and the agent
answers fluently from memory instead of querying. It looks like a working
chatbot; it is a broken agent.

**Census sentinels.** `-666666666` means suppressed. The mirrored data already
uses proper NULLs, and the choropleth relies on that — a surviving sentinel would
land in the lowest-income bucket and paint as genuine extreme poverty. Re-check
if you repoint `CENSUS_SOURCE_BASE`.

**Demographics are block-group only.** State, county and tract are boundaries
with no attributes. Medians do not aggregate, so there is no true county median
income in this bundle — see docs/data.md before adding a coarser choropleth.

**`/tiles/` belongs to the MCP server**, not TiTiler, which lives at
`/titiler/`. Swapping them breaks hex pyramids.

**An absent config key is not a disabled feature.** Upstream computes
`geoCfg.enabled !== false`, so omitting the `geocoder` block from
`layers-input.json` *registers* the `geocode` agent tool and the agent calls
Nominatim mid-conversation. Check the upstream default before trusting an
absence. This is the only class of bug here that the build-time offline gate
cannot see, because nothing is fetched until a user asks the right question.

**Missing tools in a minimal base image.** `for f in $(find ...)` with no
`find` on PATH is an empty loop, and `if grep -q` with no `grep` is a false
branch. Neither trips `set -e`. That is how `make seed` came to exit 0 after
uploading a STAC catalog full of unsubstituted `${PUBLIC_ORIGIN}` strings.
`seed/entrypoint.sh` now asserts its userland up front; extend that list if you
add a tool.

**Floating image tags.** `mcp-data-server:latest` has published as an image with
an empty `/app` and `CMD ["python3"]` — it starts a REPL, gets no stdin, exits 0,
and under `restart: unless-stopped` flaps forever logging nothing. Pin version
tags for anything load-bearing.

**nginx resolves upstreams at startup.** A literal hostname in `proxy_pass` that
does not resolve is a fatal config error, so one absent service (usually `llm`)
took down the whole front door and served nothing. Every `proxy_pass` goes
through a variable plus `resolver 127.0.0.11`, which defers resolution to request
time. Keep it that way, and note that variable form disables the implicit
prefix-stripping — hence the explicit `rewrite ... break` in each location.

**`.env` is parsed two ways.** docker compose has its own parser; the Makefile
and `scripts/*.sh` `source` it as shell. Shell sourcing aborts at the first
syntax error, so one unquoted value with a space silently unsets every variable
below it. Quote anything with spaces or parentheses.

**Upstream services do not agree on ports.** TiTiler's image binds `${PORT}`,
defaulting to 80, not 8000 — the compose file sets `PORT: 8000` so the route map
in `nginx.conf.template` stays true. Check, don't assume.

**Headers leak downstream.** rustfs speaks S3 and parses any inbound
`Authorization` header as SigV4, so a Basic credential from a proxy in front
makes `/data/` — and only `/data/` — return 400 while everything else works.
nginx clears it on that route. More generally: think about what the browser and
any fronting proxy add to a request before it reaches a service that has opinions
about headers.

**Healthchecks need a binary that exists.** `maptiler/tileserver-gl` ships
neither `curl` nor `wget`, so a curl probe pins it at `unhealthy` while it serves
tiles perfectly — a false alarm that sends you debugging the wrong thing. It has
`node`; the MCP image has `python` but no `curl`.

## Changing the geo-agent version

`patch-geoagent.mjs` asserts every pattern it rewrites and fails the build on a
miss. That is intentional — a skipped patch is an app that reaches for the
internet later, which is worse than a build that stopped.

1. Bump `GEO_AGENT_REF` in `.env`.
2. `make images`; read the failures.
3. Update patterns against the new upstream source.
4. Review the "UNREVIEWED external URLs" list; patch or allowlist with a reason.
5. `make up && make verify`, then check DevTools with the network off.

You can test the patcher without Docker:

```bash
git -C /path/to/geo-agent archive v3.25.0 app | tar -x -C /tmp/t
node app/vendor/patch-geoagent.mjs /tmp/t/app
```

## Conventions

Comments explain *why*, especially where the reason is a failure mode you
cannot see in the code. This repo is mostly a collection of small decisions that
each prevent one specific silent failure; a comment that only restates the code
is not worth its line.

Shell scripts: `set -euo pipefail`, and fail loudly with a message naming the
fix. Anything requiring network access says so in its header.
