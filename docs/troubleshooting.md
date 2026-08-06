# Troubleshooting

Start with `make verify` — it probes every service through the public origin,
which is the path the browser takes, and most entries below are things it will
name directly.

## Nothing responds on port 8080 at all

Check whether nginx is even running:

```bash
docker compose ps webapp
docker compose logs webapp | tail -20
```

`host not found in upstream` means nginx could not resolve one of the services it
proxies. nginx resolves literal upstream hostnames at *startup* and treats a
failure as fatal, so historically one absent service — usually `llm`, the slowest
to start and the most likely to be skipped — stopped the entire front door and
served nothing at all. Every `proxy_pass` now goes through a variable plus an
explicit `resolver`, which defers resolution to request time: a missing service
costs a 502 on its own route and nothing else. If you see this error after
editing `nginx.conf.template`, a literal hostname crept back in.

## `make seed` succeeds but the agent says it has no datasets

Check the hrefs actually got substituted:

```bash
curl -s http://localhost:8080/data/geo-data/stac/catalog.json | grep -c 'PUBLIC_ORIGIN'
docker compose logs mcp | grep 'Datasets loaded'
```

A literal `${PUBLIC_ORIGIN}` in the output means the substitution did not run.
The seed job now refuses to upload in that case rather than exiting 0 with a
broken catalog, so an older bundle in the bucket is the likely cause — re-run
`make seed`, then `docker compose restart mcp`, since the MCP server reads the
catalog once at startup.

`Datasets loaded: 0` with clean hrefs means the MCP server could not fetch the
collections it was pointed at. Check `STAC_CATALOG_URL` resolves from inside the
container.

## `make data` fails or stops partway

`make data` mirrors ~3 GB, so the usual cause is a dropped transfer. It is safe
to just re-run: each file is skipped when its size already matches the source,
and partial downloads live in `.part` files that are only renamed on completion,
so an interrupted run never leaves a truncated archive behind.

If it reports a missing asset key, the upstream collection changed shape — update
`LAYERS` in [`data/mirror.py`](../data/mirror.py).

There is no Census API key involved; if you find one referenced anywhere, it is
left over from the old from-scratch pipeline. See [data.md](data.md).

## A variable in `.env` seems to be ignored

`.env` is read two ways: docker compose parses it itself, and the shell scripts
plus the Makefile `source` it. Shell sourcing stops at the first syntax error, so
an unquoted value containing spaces or parentheses silently unsets **everything
below that line**. Quote such values:

```
LLM_MODEL_LABEL="Qwen3 8B (local)"
```

Check what actually survives with:

```bash
bash -c 'set -a; . ./.env; set +a; env | sort'
```

## The map loads but no layers appear

Almost always `PUBLIC_ORIGIN`.

`layers-input.json` uses relative paths, so it loads fine from any address. The
STAC catalog uses absolute hrefs baked in at seed time. If those disagree with
the address bar, the app fetches the catalog successfully and then fails on
every child collection.

```bash
grep PUBLIC_ORIGIN .env
curl -s http://localhost:8080/data/geo-data/stac/catalog.json | head -20
```

The hrefs must start with the URL you are actually browsing. Fix `.env`, then
`make seed`. (Seeding is idempotent — re-run it as often as you like.)

If the hrefs still contain a literal `${PUBLIC_ORIGIN}`, the seed job never ran.

## One layer is missing, the others work

The `vector:layers` field on that collection's `pmtiles` asset is missing or
wrong. The app falls back to the asset key as the source-layer name, MapLibre
finds no matching layer in the archive, and renders nothing — with no error.
See [data.md](data.md#adding-your-own-data) for how to read the real layer name
out of the archive.

## The map is grey; only my data is visible

The basemap container. Check `docker compose logs basemap`, then:

```bash
make basemap-verify
```

If tiles return 200 with a valid but *blank* PNG, the style generated correctly
but resolved no data — that is the `SOURCE_URL` scheme mismatch described in
[basemap/README.md](../basemap/README.md), and it is a one-line fix. For
calibration: a blank render of these styles is about 1.8 KB, and a tile with land
in it runs 15–30 KB.

If tiles return **HTTP 500**, check the log for `ENOENT` on a sprite path. A
sprite that fails to load takes the whole render down; the usual cause is a
doubled path (`/data/sprites/sprites/v4/...`) from the style's `sprite` value
being written relative to the wrong root. See
[basemap/README.md](../basemap/README.md).

If glyphs 404, `basemap/data/fonts` was not populated: `make basemap`.

If the container sits at `unhealthy` while tiles serve perfectly, the healthcheck
is wrong rather than the service — `maptiler/tileserver-gl` ships neither `curl`
nor `wget`, so the probe has to use `node`.

## The chat sends a message and nothing comes back

Check the model finished loading. Several gigabytes of weights on a cold CPU
takes minutes, and the container reports unhealthy the whole time:

```bash
docker compose logs -f llm
curl -s http://localhost:8080/api/llm/models
```

If a model is listed and chat still hangs, watch for the request in DevTools.
A response that arrives all at once after a long silence rather than streaming
means proxy buffering — check `proxy_buffering off` survived in the
`/api/llm/` block.

## The agent talks but never runs a query or touches the map

This is the `--jinja` failure, and it is the most confusing one because the
model appears to be working perfectly.

Without `--jinja`, llama.cpp ignores the model's chat template and emits no
`tool_calls`. The agent has no way to reach its tools, so it does the only
thing left: it answers from memory, fluently and often plausibly. Confirm the
flag is in the `llm` command in `docker-compose.yml`.

If the flag is there, you may be on a model whose tool calling is weak. Qwen3
instruct models work; Qwen-Coder and Gemma variants are much worse at
multi-turn tool selection.

## Charts fail, or the draw tool never opens

An unvendored lazy dependency. These load on first use, not at page load, so
they are invisible until someone presses the button.

```bash
curl -sI http://localhost:8080/vendor/d3/d3.min.js
curl -sI http://localhost:8080/vendor/plot/plot.umd.min.js
curl -sI http://localhost:8080/vendor/mapbox-gl-draw/mapbox-gl-draw.js
```

All three should be 200. If the browser is instead requesting `cdn.jsdelivr.net`
for them, the geo-agent patch did not apply — rebuild the webapp image and read
the `patch-geoagent.mjs` output.

## Queries fail with a DuckDB extension error

The MCP image bakes `httpfs`, `spatial` and `h3` into `~/.duckdb/extensions`.
If you swapped `MCP_TAG` for an image that does not, DuckDB will try to fetch
them at first use and fail with no network. Use the upstream image, or
pre-install the extensions in your own.

## Queries fail with an S3 or access-denied error

The `geo-data` bucket must be anonymously readable — `mcp-data-server`'s
default DuckDB secret is anonymous. Re-run `make seed`, which reapplies the
policy, and check `S3_SOURCES` in `docker-compose.yml` still points at your
actual `PUBLIC_ORIGIN`.

## Hex tile builds fail

`register_hex_tiles` writes to the `tiles` bucket, which unlike `geo-data`
requires credentials. It gets them from the scoped secret in `S3_SOURCES`. If
you changed `S3_ACCESS_KEY` or `S3_SECRET_KEY` after first start, the rustfs
volume still has the old ones — `make clean` (which drops the volume) and
re-seed.

## The webapp image will not build

Read the failure. There are three distinct ones, and each says what it wants:

- **`FAILED (integrity)`** in `fetch.sh` — an upstream file changed. Update the
  URL and digest together in `manifest.tsv`. Never "fix" it by setting `-`.
- **`NOT FOUND` / `AMBIGUOUS`** in `patch-geoagent.mjs` — `GEO_AGENT_REF` moved
  and a patched constant changed. See [offline.md](offline.md#when-you-bump-geo_agent_ref).
- **`OFFLINE GATE FAILED`** — something in the snapshot still points at a CDN.
  The paths are listed; add a patch or a manifest row.

## Everything is slow

Expected on CPU: roughly 5–15 tokens/second from an 8B model. The stack is
sized for one user.

If it is slower than that, check `LLM_THREADS` in `.env` matches your physical
core count, and consider dropping `LLM_CTX` to 16384 — 32k of KV cache is about
2 GB, and on a 16 GB machine that can be the difference between fitting and
swapping.

## Starting over

```bash
make clean     # containers + object store volume; keeps downloaded artifacts
make up && make seed
```

`make clean` deliberately does not delete `llm/models`, `basemap/data`,
`data/bundle` — that is a multi-gigabyte download. Remove it by
hand if you really mean it.
