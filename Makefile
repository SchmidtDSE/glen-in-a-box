# glen-in-a-box
#
# The whole project splits into two phases, and the targets are grouped to
# match. Everything under BUILD needs a network connection and is run once;
# everything under RUN never touches the internet.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose
AIRGAP  := $(COMPOSE) -f docker-compose.yml -f docker-compose.airgap.yml

# Containerised so the host needs nothing but Docker — no node, no GDAL, no
# tippecanoe, no Python.
NODE_RUN := docker run --rm -v "$(PWD)/basemap:/basemap" -w /basemap node:22

.PHONY: help
help:
	@echo ""
	@echo "  glen-in-a-box — a geo-agent that runs with the network unplugged"
	@echo ""
	@echo "  BUILD (needs internet, run once)"
	@echo "    make bundle        everything below, in order — start here"
	@echo "    make images        build and pull all container images"
	@echo "    make model         download the LLM weights (~5 GB)"
	@echo "    make basemap       extract the offline basemap (~1-2 GB)"
	@echo "    make data          mirror the census bundle (~3 GB)"
	@echo ""
	@echo "  RUN (no internet needed)"
	@echo "    make up            start the stack"
	@echo "    make seed          load the data bundle into object storage"
	@echo "    make verify        check every service end to end"
	@echo "    make logs          follow logs"
	@echo "    make down          stop"
	@echo ""
	@echo "  AIR GAP"
	@echo "    make airgap        save images + data to one transferable archive"
	@echo "    make load          restore that archive on the target machine"
	@echo "    make up-airgap     start with outbound networking disabled"
	@echo ""
	@echo "  Full first run:  cp .env.example .env && make bundle && make up && make seed"
	@echo ""

.env:
	@cp .env.example .env
	@echo "Created .env from .env.example — review it before continuing."

# ─────────────────────────────────────────────────────────────────────────────
# BUILD — online
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: bundle
bundle: .env images model basemap data
	@echo ""
	@echo "Bundle complete. From here on nothing needs the internet:"
	@echo "  make up && make seed && make verify"

.PHONY: images
images: .env
	@echo "==> pulling runtime images"
	$(COMPOSE) pull --ignore-buildable
	@echo "==> building webapp (vendors every frontend dependency) and seed"
	$(COMPOSE) build webapp
	$(COMPOSE) --profile bootstrap build seed

.PHONY: model
model: .env
	@set -a; . ./.env; set +a; bash llm/fetch-model.sh

.PHONY: basemap
basemap: .env
	@echo "==> building offline basemap (long; see basemap/README.md)"
	$(NODE_RUN) bash build.sh

.PHONY: data
data: .env
	@echo "==> mirroring the census bundle (~3 GB)"
	docker build -t glen-in-a-box/data-mirror:local ./data
	mkdir -p data/bundle
	# Re-running is cheap: mirror.py skips any file whose size already matches
	# the source, so an interrupted download resumes at file granularity.
	@set -a; . ./.env; set +a; \
	docker run --rm \
		-v "$(PWD)/data/bundle:/out" \
		-e ONLY_LAYER="$${ONLY_LAYER:-}" \
		$${CENSUS_SOURCE_BASE:+-e CENSUS_SOURCE_BASE="$$CENSUS_SOURCE_BASE"} \
		glen-in-a-box/data-mirror:local

# ─────────────────────────────────────────────────────────────────────────────
# RUN — offline
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: up
up: .env preflight
	$(COMPOSE) up -d
	@echo ""
	@echo "Starting. The LLM container loads several GB of weights and will"
	@echo "report unhealthy for a minute or two — that is expected."
	@echo "Then: make seed (first run only), then open the app."

.PHONY: up-airgap
up-airgap: .env preflight
	@echo "==> starting with outbound networking disabled"
	$(AIRGAP) up -d

.PHONY: seed
seed: .env
	$(COMPOSE) --profile bootstrap run --rm seed

.PHONY: down
down:
	$(COMPOSE) down

.PHONY: logs
logs:
	$(COMPOSE) logs -f --tail=100

.PHONY: ps
ps:
	$(COMPOSE) ps

# Catches the three mistakes that otherwise surface as a confusing runtime
# failure: missing model weights, missing basemap, missing data bundle.
.PHONY: preflight
preflight:
	@bash scripts/preflight.sh

.PHONY: verify
verify:
	@bash scripts/verify.sh

.PHONY: basemap-verify
basemap-verify:
	@bash scripts/verify.sh basemap

# ─────────────────────────────────────────────────────────────────────────────
# AIR GAP
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: airgap
airgap:
	@bash scripts/airgap-save.sh

.PHONY: load
load:
	@bash scripts/airgap-load.sh

# ─────────────────────────────────────────────────────────────────────────────

.PHONY: clean
clean:
	$(COMPOSE) down -v
	@echo "Containers and the object-store volume are gone."
	@echo "Downloaded artifacts were NOT deleted (they take hours to rebuild):"
	@echo "  llm/models  basemap/data  data/bundle"
	@echo "Remove them by hand if you really mean it."
