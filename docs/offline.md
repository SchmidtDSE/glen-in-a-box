# The offline guarantee

## What is actually claimed

**Once `make bundle` has run, no component of the running system requires a
network connection.** Build is online; run is not.

That is a narrower claim than "offline" sometimes implies, and it is worth
being precise, because the interesting question is not whether it works on a
disconnected laptop today — it is whether you can *tell* when it has quietly
stopped being true.

## Three layers of enforcement

### 1. Fetch-time verification

Every third-party library is downloaded once, at image build time, and checked
against a pinned sha384 digest in [`app/vendor/manifest.tsv`](../app/vendor/manifest.tsv).
A mismatch fails the build and prints both digests, so a legitimate version
bump is a one-line copy-paste and a tampered download is unmistakable.

This is where the trust boundary lives. The served HTML deliberately carries no
`integrity=` attributes: SRI protects bytes crossing the network from a
third-party CDN, and by then these bytes are same-origin files the image itself
shipped. Re-checking them adds nothing and creates a way for a legitimate
rebuild to fail.

### 2. Build-time assertions

[`patch-geoagent.mjs`](../app/vendor/patch-geoagent.mjs) rewrites every external
URL geo-agent hardcodes. Each rewrite asserts that its pattern is present
exactly once. Zero matches fails the build; more than one also fails, because
an unexpected second occurrence means the file changed shape and the patch may
be landing somewhere it was never meant to.

It then sweeps every `.js` file in the snapshot for surviving `http(s)://`
references and reports any that are not on a reviewed allowlist — each entry of
which records *why* that URL can never be reached (dead config default,
comment, disabled feature). When `GEO_AGENT_REF` is bumped and upstream adds a
new external call, this is what surfaces it.

Finally, [`app/Dockerfile`](../app/Dockerfile) greps the finished snapshot for
known CDN hostnames and refuses to produce an image if any survive.

### 3. Run-time isolation

```bash
make up-airgap
```

applies [`docker-compose.airgap.yml`](../docker-compose.airgap.yml), which sets
`internal: true` on the shared network. That strips the containers' default
route: anything still trying to reach the internet fails loudly instead of
quietly working on a connected dev machine and breaking in the field. Published
ports are unaffected.

## Verifying the browser half

The three mechanisms above cover the **server**. They say nothing about the
**browser**, which runs on your host with its own network stack. A page could
in principle request something the container never would.

To check that half, there is no substitute for looking:

1. `make up`
2. Open the app, open DevTools → Network, and check "Disable cache".
3. Hard-reload. Exercise everything: pan and zoom the map, toggle all three
   basemaps, run a query, **ask for a chart**, open the draw tool, export a map.
4. Sort by Domain. Every request should be to `PUBLIC_ORIGIN`.

Then the real test: turn the machine's wifi off and do it again.

Step 3 matters more than it looks. Several dependencies — d3, Observable Plot,
mapbox-gl-draw — load lazily and only when a feature is first used. A page that
looks completely clean on load can still have an unvendored dependency hiding
behind a button nobody pressed.

## Known escape hatches

Honest accounting of things that still point outward:

| What | Behaviour offline | Why it is left alone |
|---|---|---|
| Footer "docs" / "github" links | Dead links | User-initiated navigation, not an app dependency |
| Exported standalone HTML | Works on this box; broken if emailed elsewhere | Patched to `/vendor/`, which only resolves here. Fully self-contained export would mean inlining ~1 MB of JS into every file |
| Geocoder, transcription, upload | Absent from the UI | Disabled in config; no offline equivalent exists |

## When you bump `GEO_AGENT_REF`

Expect the build to fail. That is the design.

1. `make images` — `patch-geoagent.mjs` reports which patterns no longer match.
2. Compare against the new upstream source, update the patterns.
3. Check the "UNREVIEWED external URLs" list for anything new.
4. Re-run, then `make verify` and repeat the browser check above.

Do not work around a failing patch by deleting it. An unpatched constant is an
app that reaches for the internet at some unpredictable later moment, which is
far worse than a build that stopped.
