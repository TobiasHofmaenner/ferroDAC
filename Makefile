# ferroDAC dev tasks. `make help` lists targets.
.DEFAULT_GOAL := help
QT := QT_QPA_PLATFORM=offscreen

.PHONY: help test test-core test-ui test-int run hub codegen

help:  ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

test:  ## run the whole suite (data-plane + gRPC + UI, offscreen Qt)
	$(QT) pytest -ra

test-core:  ## fast gate: Qt-free data-plane + in-process gRPC e2e
	pytest -m "not ui" -ra

test-ui:  ## UI smoke tests only (offscreen Qt)
	$(QT) pytest -m ui -ra

test-int:  ## the real-gRPC end-to-end tests only
	pytest -m integration -ra

run:  ## launch the app
	python -m ferrodac

hub:  ## build + run the hub container (from server/)
	cd server && docker compose up -d --build

codegen:  ## regenerate the gRPC stubs from the .proto (dockerised protoc)
	sh server/proto/gen.sh
