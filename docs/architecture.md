# Architecture

Six containers, one published port. This document covers the decisions that are
not obvious from reading `docker-compose.yml`.

## The single-origin rule

`webapp` publishes port 8080. Nothing else publishes anything. Every other
service is reachable only through an nginx `location` block.

This is not a security posture — it is what makes the rest of the system
possible.

**It eliminates CORS.** There is not one `Access-Control-Allow-Origin` header
in this repo. The browser only ever talks to one origin, so there is nothing to
allow.

**It resolves the STAC href problem.** A STAC asset href is read twice: once by
the browser, which fetches PMTiles over HTTP range requests, and once by the
MCP server, which rewrites it to `s3://` for DuckDB. Those two consumers live
on different sides of the container boundary. Without a shared front door,
`http://rustfs:9000/...` works for the server and fails in the browser, and
`http://localhost:9000/...` does the reverse. With one, `${PUBLIC_ORIGIN}/data/...`
is correct for both — the browser follows it directly, and the MCP server is
taught to rewrite that prefix back to `s3://` via `S3_SOURCES`.

**It gives relocation one knob.** Move the box to a LAN address, change
`PUBLIC_ORIGIN`, re-seed. No rebuild.

### Route map

| Path | Upstream | Notes |
|---|---|---|
| `/` | static | app shell, `no-store` |
| `/vendor/`, `/app/` | static | immutable, 30-day cache |
| `/api/llm/` | `llm:8080/v1/` | streaming, 600 s timeout |
| `/mcp` | `mcp:8000/mcp` | SSE, unbuffered, 600 s |
| `/tiles/` | `mcp:8000/tiles/` | hex pyramids — **not** TiTiler |
| `/titiler/` | `titiler:8000/` | COG rasters |
| `/data/` | `rustfs:9000/` | range requests, unbuffered |
| `/basemap/` | `basemap:8080/` | rendered tiles + glyphs, 1-day cache |

The `/tiles/` versus `/titiler/` split catches people. `mcp-data-server` mounts
its own `/tiles/{namespace}/{name}/{z}/{x}/{y}.pbf` route for hex pyramids
built by `register_hex_tiles`, and advertises those URLs to the browser using
`MCP_PUBLIC_BASE_URL`. TiTiler cannot have `/tiles/`; it would shadow them.

Two proxy details are load-bearing. `/data/` must pass `Range` and `If-Range`
through with buffering off, because PMTiles is read entirely by range request —
a buffering proxy would pull a whole multi-hundred-megabyte archive to serve one
tile. `/api/llm/` and `/mcp` must also be unbuffered, or streamed tokens arrive
in one lump after a long silence and the UI looks hung.

## Storage and access asymmetry

Two buckets with deliberately different policies:

- **`geo-data`** — anonymous read. Forced by `mcp-data-server`: its default
  DuckDB S3 secret is anonymous, so anything it reads without a scoped secret
  must allow anonymous GET.
- **`tiles`** — credentialed read/write. `register_hex_tiles` writes here, and
  an anonymously writable bucket on a LAN-visible box is a bad trade. It gets a
  prefix-scoped secret through `S3_SOURCES`.

DuckDB extensions (`httpfs`, `spatial`, `h3`) are already baked into
`~/.duckdb/extensions` in the upstream image. This matters more than it sounds:
DuckDB fetches extensions from a remote repository on first use, so an image
without them pre-installed dies on the first query in an air gap.

## Why llama.cpp, and why `--jinja`

The agent loop is entirely tool-driven. Every question becomes MCP calls that
browse the catalog, run SQL, and restyle layers. A model that converses well
but emits malformed tool calls is useless — it degrades into a chatbot that
cannot see the data.

`--jinja` makes llama.cpp honour the model's own chat template, which is what
produces `tool_calls`. Without it the server silently emits none. It is the
single most important flag in the compose file.

Qwen3-8B at Q4_K_M is the smallest configuration that holds up across multiple
tool-calling turns. Two tempting alternatives that do not work as well:
Qwen-Coder is tuned for code completion and is measurably worse at multi-turn
tool selection, and Gemma has no native tool-call tokens at all — its tool use
is prompt-emulated and brittle in exactly the multi-step case this app needs.

Context size is a real constraint. The MCP `query` tool returns substantial
optimization guidance with every response, so 32k is the default and 16k is the
practical floor. KV cache is quantized to q8_0 to keep 32k affordable.

## Startup ordering

`rustfs` has a healthcheck and everything that depends on it waits. `mcp` runs
with `STAC_ALLOW_DEGRADED_START=true` so a slow object store start does not
wedge it into a crashloop; it retries the catalog in the background.

`llm` has a 300-second `start_period`, because loading several gigabytes of
weights on a cold CPU is genuinely slow and a shorter grace period would kill
it mid-load, forever.

Nothing depends on `seed` — it runs under the `bootstrap` profile and only when
you ask for it.

## Resource tuning

Upstream defaults are sized for a cluster and will thrash a laptop.
`docker-compose.yml` overrides `TILE_BUILD_THREADS` (48 → 4), `TILE_THREADS`,
`MCP_QUERY_CONCURRENCY` and `DUCKDB_MEMORY_LIMIT`. nginx's `worker_processes` is
pinned to 2 in the entrypoint for the same reason — `auto` means one worker per
core, which on a high-core machine is hundreds of processes in a container that
serves a handful of users.
