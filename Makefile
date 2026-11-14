SHELL := /bin/bash
CP ?= CP0
PORT ?= 8080
HOST ?= 127.0.0.1
WEB_PORT ?= 5173

.PHONY: setup contracts contracts-check test cp0 cp1-a cp1-joint cp2-a checkpoint api web smoke
setup:
	uv sync --locked
	npm --prefix web ci

contracts:
	uv run python scripts/export_contracts.py
	npm --prefix web run generate:contracts

contracts-check:
	uv run python scripts/check_generated.py

test:
	uv run ruff check src scripts tests --no-cache
	uv run pytest

cp0: contracts-check test
	npm --prefix web run test:ui
	npm --prefix web run build
	uv run python scripts/check_cp0.py
	uv run python scripts/smoke_cp0.py

cp1-a: contracts-check test
	uv run python scripts/accept_cp1_a.py

cp1-joint: contracts-check test
	uv run python scripts/accept_cp1_joint.py

cp2-a:
	uv run python scripts/accept_cp2_a.py

checkpoint:
	uv run python scripts/check_checkpoint.py $(CP)

api:
	uv run uvicorn campus_fabric.app:create_app --factory --host $(HOST) --port $(PORT)

web:
	FABRIC_API_TARGET=http://127.0.0.1:$(PORT) npm --prefix web run dev -- --port $(WEB_PORT)

smoke:
	uv run python scripts/smoke_cp0.py
