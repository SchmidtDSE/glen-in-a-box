# glen-in-a-box

A complete [geo-agent](https://github.com/boettiger-lab/geo-agent) deployment
that runs with the network cable unplugged. Ask questions about US census data
in plain language, get SQL answers and styled map layers back — with the
language model, the database, the tile server, the object store and every
JavaScript byte running on one machine.

Built for field sites, secure facilities, conference demos on hotel wifi,
classrooms behind a captive portal, and anywhere else "it works on my laptop"
has to mean the whole thing.

```
                          ┌─ your browser ─┐
                          │  ONE origin    │
                          └───────┬────────┘
                                  │ :8080
                        ┌─────────▼─────────┐
                        │  webapp  (nginx)  │  app + vendored JS/CSS
                        └─┬───┬───┬───┬───┬─┘
              /api/llm/ ──┘   │   │   │   └── /basemap/
                     /mcp ────┘   │   └────── /data/
                                  └────────── /titiler/
     ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
     │   llm    │  │   mcp    │  │  titiler │  │  rustfs  │  │ basemap  │
     │llama.cpp │  │ DuckDB   │  │   COG    │  │    S3    │  │tileserver│
     └──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘
```

---

## Quick start

Requires Docker with Compose v2. Nothing else — no Node, no Python, no GDAL on
the host.

```bash
git clone https://github.com/SchmidtDSE/glen-in-a-box
cd glen-in-a-box
cp .env.example .env

make bundle     # ONLINE, once. ~2 hours, ~10 GB downloaded.
make up
make seed       # first run only
make verify
```

Then open <http://localhost:8080>.

After `make bundle`, the internet is never needed again. To prove that, unplug
and run `make up-airgap`.

### Hardware

| | Minimum | Comfortable |
|---|---|---|
| RAM | 16 GB | 32 GB |
| Disk | 25 GB | 40 GB |
| CPU | 4 cores | 8+ cores |
| GPU | not required | NVIDIA, via `--profile gpu` |

On CPU, expect roughly 5–15 tokens/second from the 8B model — fine for one
person asking questions, slow if several people share the box. A GPU makes it
feel instant.

---

## The two phases

This is the single most important thing to understand about the project.

**Build is online. Run is offline.** "Fully offline" does not mean the
artifacts materialize from nothing; it means that once they exist, the running
system has no dependency on any network. `make bundle` is the one online step:

| Step | Downloads | Size |
|---|---|---|
| `make images` | container images; the webapp build vendors every frontend library | ~2 GB |
| `make model` | GGUF weights from Hugging Face | ~5 GB |
| `make basemap` | US extract of the Protomaps planet build, plus glyphs | ~1–2 GB |
| `make data` | TIGER/Line geometry and ACS tables, built into parquet + PMTiles | ~1–2 GB |

To move a built box somewhere with no connection at all:

```bash
make airgap     # on a connected machine → airgap/{images,payload}.tar.zst
# copy airgap/ and this repo to the target
make load       # on the target
make up && make seed
```

---

## What "offline" actually took

Most of the work in this repo is in the places a normal geo-agent deployment
quietly reaches for the internet. Documented here because every one of them is
a trap someone else will otherwise rediscover.

### Every frontend byte is vendored

The standard template loads MapLibre, PMTiles, h3-js, marked, DOMPurify,
highlight.js, the MCP SDK and geo-agent itself from four different CDNs. All of
it is snapshotted into the webapp image at build time by
[`app/vendor/`](app/vendor/), verified against pinned sha384 digests on the way
in, and served same-origin thereafter.

Two of these are not obvious:

- **d3 and Observable Plot load lazily**, from inside `chart-renderer.js`, the
  first time someone asks for a chart. A bundle that skips them looks perfectly
  healthy for as long as nobody wants a bar chart. Same story for
  `mapbox-gl-draw` and the draw tool.
- **The MCP SDK cannot be curl'd.** The import map points at esm.sh, whose
  modules carry transitive imports back to esm.sh. `bundle-mcp-sdk.sh` installs
  the real npm package and bundles it with esbuild, using code splitting so
  both entry points share one module instance — separate instances handshake
  and then silently do nothing.

### geo-agent is patched, and the patches are assertions

A handful of URLs are baked into geo-agent's module scope with no config
surface: the MapLibre glyph endpoint, two of three basemaps, the lazy library
loads, and the upload bucket.
[`patch-geoagent.mjs`](app/vendor/patch-geoagent.mjs) rewrites all of them —
and **fails the build** if any expected pattern is missing.

That strictness is the point. A best-effort patcher that skipped a moved
constant would produce an image that boots fine, demos fine, and then reaches
for the internet at some unpredictable later moment. A failed build is much
cheaper. A final gate in the Dockerfile greps the whole snapshot for CDN
hostnames and refuses to ship if any survive.

### The basemap had no fallback

All three of MapLibre's built-in basemaps are remote, and the glyph endpoint is
hardcoded to `demotiles.maplibre.org`. Without a local replacement the map is a
grey rectangle. [`basemap/`](basemap/) extracts a US window from the Protomaps
daily planet build and renders raster tiles from it locally. See
[basemap/README.md](basemap/README.md).

### One origin, no CORS

Every service is reachable only through nginx. This kills CORS entirely, and it
solves a subtler problem: a STAC href has to be resolvable both by the browser
and by the MCP server, and `rustfs:9000` and `localhost:9000` cannot both be
right. With a single front door there is one URL that works for both.

### Turned off, on purpose

| Feature | Why |
|---|---|
| Geocoder | Nominatim and Photon are both remote; there is no offline equivalent |
| Voice transcription | Needs a speech model this box does not ship |
| Upload | Writes to object storage that is read-only here |
| Satellite basemap | Offline imagery would be tens of gigabytes; the button shows a dark cartographic style instead |
| Carbon dashboard link | Points at a Nautilus service |

---

## What is in the box

Three census layers, each as both vector tiles (for the map) and parquet (for
SQL), built from TIGER/Line 2024 geometry joined to ACS 2023 five-year
estimates:

- **States** — 56 features
- **Counties** — ~3,200 features
- **Census tracts** — ~85,000 features

Each carries population, median household income, median home value, poverty
counts, and derived population density and poverty rate. Full column
documentation is in the generated STAC collections; the agent reads it at
runtime via `get_schema`.

Two things the build gets right that are easy to get wrong: the Census API's
`-666666666` "suppressed" sentinels are converted to NULL rather than left to
poison every average and choropleth break, and everything joins on GEOID
because county names are not unique across states.

---

## Making it your own

This is a template. The three files you will actually edit:

| File | What it controls |
|---|---|
| [`app/layers-input.json`](app/layers-input.json) | which layers appear, how they are styled, the welcome text |
| [`app/system-prompt.md`](app/system-prompt.md) | the agent's persona, domain context, and guardrails |
| [`data/build.py`](data/build.py) | what data goes in the box |

To swap in your own data, replace `build.py` with something that writes the
same shape into `data/bundle/`: parquet, PMTiles, and STAC collections whose
hrefs use the literal `${PUBLIC_ORIGIN}` placeholder. The seed job substitutes
it at upload time, which is what lets one bundle serve localhost, a LAN
address, or someone else's reverse proxy without rebuilding.

Keep the system prompt lean. Column names and paths come from the MCP tools at
runtime; repeating them in the prompt just gives the agent two sources of truth
that will disagree.

### Deploying on a LAN

Set `PUBLIC_ORIGIN` to the host's address, not `localhost`, and re-run
`make seed`. There is no TLS and no authentication — anyone who can reach the
port can use the model and read the data. That is a reasonable trade for a
single-user box or a closed network, and not one for anything wider.

---

## Documentation

- [docs/architecture.md](docs/architecture.md) — how the services fit together and why
- [docs/offline.md](docs/offline.md) — the offline guarantee and how to verify it yourself
- [docs/data.md](docs/data.md) — the census pipeline, and how to add your own layers
- [docs/troubleshooting.md](docs/troubleshooting.md) — symptoms, causes, fixes
- [basemap/README.md](basemap/README.md) — the offline basemap
- [AGENTS.md](AGENTS.md) — orientation for AI coding agents working in this repo

Upstream: [geo-agent](https://github.com/boettiger-lab/geo-agent) ·
[docs](https://boettiger-lab.github.io/geo-agent/) ·
[mcp-data-server](https://github.com/boettiger-lab/mcp-data-server)

---

## Attribution and licensing

This repository is MIT licensed. The things it downloads are not, and their
terms travel with the box you build:

- **Basemap** — OpenStreetMap data via Protomaps, under
  [ODbL](https://www.openstreetmap.org/copyright). Attribution is required and
  is wired into the map corner; leave it there.
- **Census data** — US Census Bureau, public domain (CC0).
- **Model weights** — under the license of whichever model you fetch. Qwen3 is
  Apache 2.0.
- **geo-agent, MapLibre, and the vendored libraries** — their own licenses,
  preserved in the snapshot.
