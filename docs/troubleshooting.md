# Troubleshooting

Start with `make verify` — it probes every service through the public origin,
which is the path the browser takes, and most entries below are things it will
name directly.

## `make up-airgap` publishes no port

That is deliberate, not a bug. `internal: true` stops published ports from
working, so there is no way in from the host — which is why the probes have to run
from inside:

```bash
make up-airgap
make verify-airgap
```

Giving webapp a second, non-internal network to carry the port looks like the fix
and is not: with one network internal, docker's embedded DNS forwards service
names to a resolver it cannot reach and answers SERVFAIL, so every proxied route
502s. Compose accepts a per-network `priority` to break the tie and this Docker
leaves `GwPriority` at 0 regardless, which is why that appeared to work once and
then stopped.

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

## Running on podman

The stack works on rootless podman, and two of its behaviours differ from Docker
in ways the repo now accommodates — both confirmed by testing, not assumed:

* **The embedded DNS is at the network gateway** (`10.89.0.1`), not `127.0.0.11`.
  `entrypoint.sh` reads it from the container's own `resolv.conf`, so nothing is
  hardcoded. With the old hardcoded address, `/data/` returned nothing at all;
  with the derived one it returns 200.
* **Exec-form healthcheck arrays are re-split on whitespace.** A probe written as
  `["CMD","python","-c","import x; ..."]` is stored as
  `["CMD","python","-c","import","x; ..."]`, python receives just `import`, and
  the container reports `unhealthy` forever. Confirmed on podman 3.4.4: the
  exec-form probe exits 1 and `CMD-SHELL` exits 0. Use `CMD-SHELL` with one
  string for any probe whose arguments contain spaces.

On **Ubuntu 22.04 specifically**, podman 3.4.4 cannot create a working network out
of the box, and this is not a fault of this project: podman writes
`"cniVersion": "1.0.0"` into the network's conflist while the packaged CNI
plugins are 0.9.1, which reject it. Containers then fail to start with

```
CNI network "<name>_default" not found
Error validating CNI config file ...: [plugin firewall does not support config version "1.0.0"]
```

Either upgrade podman to 4.x (netavark), or patch the version in the generated
conflist, which is a user-level file:

```bash
sed -i 's/"cniVersion": "1.0.0"/"cniVersion": "0.4.0"/' \
    ~/.config/cni/net.d/<network>.conflist
```

Point `docker compose` at podman with
`export DOCKER_HOST="unix:///run/user/$(id -u)/podman/podman.sock"`.

## The app shell loads but every proxied route returns 000

Distinctive signature: `make verify` passes the whole `app shell` section and then
reports `HTTP 000` for `/data/`, `/mcp`, `/titiler` and `/basemap` — each after a
long pause rather than immediately.

That is nginx's `resolver` pointing at the wrong address. Check what the container
decided:

```bash
docker compose logs webapp | grep 'DNS resolver'
docker compose exec -T webapp grep ^nameserver /etc/resolv.conf
```

The two should agree. Docker's embedded DNS answers on 127.0.0.11; podman's
aardvark-dns answers on the network gateway instead. `entrypoint.sh` reads the
address from resolv.conf so neither is hardcoded, and `DNS_RESOLVER` overrides it
if that heuristic picks wrong.

It is read once at start, so if you recreated the network without recreating the
container, the address can be stale:

```bash
docker compose up -d --force-recreate webapp
```

## Services are healthy but cannot reach each other

Symptom: every container reports healthy, the app serves static files, and every
proxied route 502s or times out. Healthchecks do not catch this because each one
probes its own process on 127.0.0.1.

Test it directly:

```bash
docker compose exec -T webapp sh -c 'wget -qO- http://rustfs:9000/health'
```

If the name resolves but the connection hangs, the compose bridge exists without
working forwarding. Recreating the network fixes it:

```bash
docker compose down
docker network rm glen-in-a-box_glen
docker compose up -d
```

Observed on a host that also runs k3s, and it correlates with *switching between
overlay sets* — `up` then `up-airgap` then `up` again — because each switch makes
compose tear down and recreate the network. Counting iptables FORWARD rules for
the bridge is not a useful diagnostic on Docker 29 (a working network shows none;
it is not using that chain). Try to settle on one overlay set for a session.

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

## A container reports unhealthy while working perfectly

Check what the healthcheck actually probes. Every healthcheck in this stack uses
a literal `127.0.0.1` rather than `localhost`, because these servers bind
`0.0.0.0` (IPv4 only) and in some images `localhost` resolves to `::1` first —
the probe is then refused while the service answers every real request. It
depends on the image's `/etc/hosts`, so it can appear after an unrelated rebuild.

The other variant is a probe using a binary the image does not ship: `curl` is
absent from `maptiler/tileserver-gl` and from `mcp-data-server`.

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

## The agent says the MCP server is unreachable, but every probe passes

Check that `config.json` gives an **absolute** `mcp_server_url`:

```bash
curl -s http://localhost:8080/config.json | grep mcp_server_url
```

The MCP client does `new URL(url)` with no base, and `new URL("/mcp")` throws.
The transport is never constructed, the error is swallowed, and the agent reports
the server unreachable — while the server is perfectly healthy and every
server-side check passes, because none of them exercises the browser's URL
construction. Everything else browser-facing in this deployment is deliberately a
relative path; this one value cannot be, so it is built from `PUBLIC_ORIGIN` at
container start. `make verify` now asserts it.

If it is absolute and the agent still cannot connect, look for a 401 on `/mcp` in
the nginx access log — a proxy in front may be rejecting it.

## Only the /data/ route fails, with 400 InvalidRequest

```
<Error><Code>InvalidRequest</Code><Message>invalid header: authorization</Message></Error>
```

An `Authorization` header reached the object store. rustfs speaks S3, so it tries
to parse any such header as AWS SigV4 and rejects a Basic one — while the app
shell, the LLM, the MCP server and the basemap all keep working. It reads as "the
data bundle is broken" and is nothing of the kind.

This appears the moment the box sits behind a reverse proxy doing basic auth, or
a browser replays credentials on same-origin requests. `nginx.conf.template`
clears the header on the `/data/` route for exactly this reason; if you are
adding an authenticating proxy, also strip it there (Traefik's basicAuth has
`removeHeader: true`). These objects are anonymously readable by design, so no
credential belongs on that route at all.

## Deploying behind a reverse proxy

Set `PUBLIC_ORIGIN` to the externally visible URL — scheme included — and re-run
`make seed`, since it is baked into the STAC hrefs. Then set `PUBLIC_BIND` to the
address the proxy reaches this box on, rather than leaving the port on `0.0.0.0`:
otherwise the proxy asks for a password and a direct hit on `http://<host>:8080`
does not, which makes the authentication decorative.

The proxy also needs generous timeouts. A tool-calling turn can run for minutes,
and nginx here uses 600s to match geo-agent's own client timeout; a proxy with a
30s default becomes the weak link and the UI looks hung.

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
