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
| [mcp-data-server](https://github.com/boettiger-lab/mcp-data-server) | SQL + STAC backend. Used as a published image, configured by env only. |
| [geo-agent-template](https://github.com/boettiger-lab/geo-agent-template) | The online template this is the offline sibling of. |

## What to edit, and what not to

**Edit freely:** `app/layers-input.json`, `app/system-prompt.md`,
`data/build.py`, the docs.

**Edit carefully:** `app/vendor/*` — this is the offline machinery. See below.

**Do not write JavaScript.** The frontend is a snapshot of geo-agent. Behaviour
changes belong upstream. The only exception is
[`patch-geoagent.mjs`](app/vendor/patch-geoagent.mjs), and only to redirect a
URL — never to change logic.

**Do not commit build artifacts:** `data/bundle/`, `data/cache/`, `llm/models/`,
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

**Tile fields versus parquet fields.** PMTiles carry only `TILE_FIELDS` from
`build.py`. A `tooltip_fields` entry naming a parquet-only column renders blank.

**Missing `--jinja`.** Without it llama.cpp emits no `tool_calls` and the agent
answers fluently from memory instead of querying. It looks like a working
chatbot; it is a broken agent.

**Census sentinels.** `-666666666` means suppressed. Nulled in `build.py` —
keep it that way, or every aggregate silently goes wrong.

**`/tiles/` belongs to the MCP server**, not TiTiler, which lives at
`/titiler/`. Swapping them breaks hex pyramids.

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
