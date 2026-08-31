# F1 Timing Dashboard - developer entrypoints
#
# Python note: this machine's Homebrew Python 3.14 cannot bootstrap pip
# (pip 26.2.1's vendored truststore crashes because platform.mac_ver() returns
# '' on macOS 26). The backend therefore pins Python 3.13. Do NOT run
# `pip install --upgrade pip` inside the venv - 26.2.1 is the broken version.

PYTHON ?= python3.13
VENV := backend/.venv
PY := $(VENV)/bin/python

.PHONY: help setup setup-backend setup-frontend dev dev-backend dev-frontend record test test-backend lint clean

help:
	@echo "make setup    - create the venv and install backend + frontend deps"
	@echo "make dev      - run backend (:8000) and frontend (:5173) together"
	@echo "make record   - run ONLY the raw recorder (no UI) - the Tier A fallback"
	@echo "make test     - run the backend test suite"
	@echo "make clean    - remove venv, node_modules and build output"

setup: setup-backend setup-frontend

setup-backend:
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install -r backend/requirements.txt
	@test -f backend/.env || (cp backend/.env.example backend/.env && \
		echo ">> created backend/.env from the example - fill in your credentials")

setup-frontend:
	cd frontend && npm install

dev-backend:
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

dev-frontend:
	cd frontend && npm run dev

# Runs both and shuts both down on Ctrl-C. The frontend proxies /ws and /health
# to the backend, so open http://localhost:5173 only.
dev:
	@echo "backend  -> http://127.0.0.1:8000"
	@echo "frontend -> http://localhost:5173   <- open this one"
	@trap 'kill 0' EXIT INT TERM; \
		( cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000 ) & \
		( cd frontend && npm run dev ) & \
		wait

# The recorder alone, with nothing else that could break it. Use this rather
# than `make dev` if all you need is a complete capture of a session.
record:
	cd backend && .venv/bin/python -m app.recorder

test: test-backend

test-backend:
	cd backend && .venv/bin/python -m pytest -q

lint:
	cd frontend && npm run lint
	cd frontend && npx tsc -b

clean:
	rm -rf $(VENV) frontend/node_modules frontend/dist
