# Webapp image: nginx plus an offline-complete snapshot of the frontend.
#
# Stage 1 is the ONLY place in this project that touches the network at build
# time, and it is deliberately fenced off: everything it downloads is pinned by
# version and (where upstream publishes one) verified against a sha384 digest.
# Stage 2 copies the result into nginx and carries no toolchain, so the shipped
# image has no way to fetch anything even if something tried.
#
# Build:  docker compose build webapp
# Bump:   change GEO_AGENT_REF, rebuild, and expect patch-geoagent.mjs to fail
#         loudly if upstream moved one of the URLs it rewrites.

# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — snapshot
# ─────────────────────────────────────────────────────────────────────────────
FROM node:22-alpine AS vendor

RUN apk add --no-cache curl git openssl

WORKDIR /build

# Third-party browser libraries, verified against pinned digests on arrival.
COPY vendor/manifest.tsv vendor/fetch.sh ./vendor/
RUN sh ./vendor/fetch.sh ./vendor/manifest.tsv /out/vendor

# The MCP SDK needs a bundler rather than curl — see the script's header.
COPY vendor/bundle-mcp-sdk.sh ./vendor/
ARG MCP_SDK_VERSION=1.12.0
RUN sh ./vendor/bundle-mcp-sdk.sh "${MCP_SDK_VERSION}" /out/vendor/mcp

# geo-agent itself. Normally loaded from jsDelivr at runtime; here it is baked
# in at a pinned tag. --depth 1 against a tag keeps this to a few hundred KB.
ARG GEO_AGENT_REF=v3.25.0
ARG GEO_AGENT_REPO=https://github.com/SchmidtDSE/glen.git
RUN git clone --depth 1 --branch "${GEO_AGENT_REF}" "${GEO_AGENT_REPO}" /tmp/geo-agent \
    && mkdir -p /out/app \
    && cp -r /tmp/geo-agent/app/. /out/app/ \
    # The upstream repo ships its own dev-mode index.html, layers-input.json and
    # system-prompt.md next to the modules. Ours live at the document root and
    # would be shadowed by these; drop them to keep exactly one of each.
    && rm -f /out/app/index.html /out/app/layers-input.json /out/app/system-prompt.md \
    && echo "${GEO_AGENT_REF}" > /out/app/GEO_AGENT_VERSION

# Rewrite the external URLs geo-agent hardcodes. Fails the build if any
# expected pattern is missing — an unpatched constant is worse than no image.
COPY vendor/patch-geoagent.mjs ./vendor/
RUN node ./vendor/patch-geoagent.mjs /out/app

# Belt-and-braces gate: no vendored or snapshotted asset may reference a CDN
# host. This is the check that would have caught the lazy-loaded chart
# libraries, which are invisible until a user asks for their first chart.
RUN if grep -rIl -E 'https?://(cdn\.jsdelivr\.net|unpkg\.com|esm\.sh|cdnjs\.cloudflare\.com|demotiles\.maplibre\.org|[a-z]\.basemaps\.cartocdn\.com|services\.arcgisonline\.com)' \
        /out/app /out/vendor 2>/dev/null | grep -v -E '\.(map|md)$' | grep . ; then \
        echo "" >&2; \
        echo "OFFLINE GATE FAILED: the files listed above still point at a CDN." >&2; \
        echo "Add a patch in vendor/patch-geoagent.mjs or a row in vendor/manifest.tsv." >&2; \
        exit 1; \
    fi \
    && echo "offline gate: no CDN references in /out/app or /out/vendor"

# Static app content, last so editing it does not re-run any of the above.
COPY index.html layers-input.json system-prompt.md /out/

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — serve
# ─────────────────────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine

# gettext supplies envsubst, used by the entrypoint to bake ${PUBLIC_ORIGIN}
# into config.json; curl/wget serve the healthcheck.
RUN apk add --no-cache gettext curl

COPY --from=vendor /out/ /usr/share/nginx/html/

COPY nginx.conf.template /etc/nginx/templates.glen/nginx.conf.template
COPY config.json.template /etc/nginx/templates.glen/config.json.template
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 80
ENTRYPOINT ["/entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
