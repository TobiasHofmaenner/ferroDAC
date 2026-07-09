# ferroDAC dev tasks. `make help` lists targets. Everything python runs through
# `uv run` (the locked .venv from pyproject/uv.lock) — never the system python.
.DEFAULT_GOAL := help
QT := QT_QPA_PLATFORM=offscreen
UV := uv run

.PHONY: help sync test test-core test-ui test-int run hub codegen render

help:  ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

sync:  ## create/refresh the locked environment (.venv)
	uv sync --all-groups

test:  ## run the whole suite (data-plane + gRPC + UI, offscreen Qt)
	$(QT) $(UV) pytest -ra

test-core:  ## fast gate: Qt-free data-plane + in-process gRPC e2e
	$(UV) pytest -m "not ui" -ra

test-ui:  ## UI smoke tests only (offscreen Qt)
	$(QT) $(UV) pytest -m ui -ra

test-int:  ## the real-gRPC end-to-end tests only
	$(UV) pytest -m integration -ra

render:  ## render panels to /tmp/ferrodac_*.png for visual QA (SCENE=all|waterfall|…)
	$(QT) $(UV) python tools/render.py $(or $(SCENE),all)

run:  ## launch the app
	$(UV) python -m ferrodac

hub:  ## build + run the hub container (from server/)
	cd server && docker compose up -d --build

codegen:  ## regenerate the gRPC stubs from the .proto (dockerised protoc)
	sh server/proto/gen.sh
