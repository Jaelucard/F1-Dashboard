# F1 Timing Dashboard - developer entrypoints
#
# Python note: the backend is pinned to Python 3.13. Python 3.14 is not yet
# supported because current pip releases cannot bootstrap inside a 3.14 venv on
# some platforms. Leave the venv's pip at the version `python -m venv` installs
# rather than upgrading it; the pinned interpreter is tested as-is.

PYTHON ?= python3.13
VENV := backend/.venv
PY := $(VENV)/bin/python

.PHONY: help setup setup-backend setup-frontend dev dev-backend dev-frontend record demo types outlines test test-backend test-frontend lint clean clean-recordings

help:
	@echo "make setup    - create the venv and install backend + frontend deps"
	@echo "make dev      - run backend (:8000) and frontend (:5173) together"
	@echo "make record   - run ONLY the raw recorder (no UI) - the Tier A fallback"
	@echo "make demo     - run with a synthetic grid, no OpenF1 connection"
	@echo "make types    - regenerate the TypeScript types and test fixture"
	@echo "make outlines - regenerate frontend/src/data/circuits from OpenF1 history"
	@echo "make test     - run backend and frontend test suites"
	@echo "make lint     - ruff (backend), eslint + tsc (frontend)"
	@echo "make clean    - remove venv, node_modules and build output"
	@echo "make clean-recordings - delete recordings/<session_key> folders, one at a time, with a y/N prompt"

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

# Runs both and shuts both down together. scripts/dev.sh refuses to start
# while :8000 or :5173 is taken (naming the pid to kill), stops the other
# process when one dies, and force-kills anything still holding a port a few
# seconds after Ctrl-C - see the comment at the top of that script for the
# uvicorn reloader deadlock that made this necessary. The frontend proxies
# /ws and /health to the backend, so open http://localhost:5173 only.
dev:
	@./scripts/dev.sh dev

# The recorder alone, with nothing else that could break it. Use this rather
# than `make dev` if all you need is a complete capture of a session.
record:
	cd backend && .venv/bin/python -m app.recorder

# Serve the synthetic grid so the UI can be checked outside a session window.
# Same launcher as `make dev`, without --reload and without OpenF1.
demo:
	@./scripts/dev.sh demo

# Regenerate everything derived from the pydantic models. A backend test
# fails if these are stale, so run this after touching app/models.py.
types:
	cd backend && .venv/bin/python -m scripts.generate_ts_types
	cd backend && .venv/bin/python -m scripts.generate_fixtures

# Regenerate the bundled circuit outlines from OpenF1's historical location
# data (no credentials needed). Commit the JSON files it writes.
outlines:
	cd backend && .venv/bin/python -m scripts.generate_outlines

test: test-backend test-frontend

test-backend:
	cd backend && .venv/bin/python -m pytest -q

test-frontend:
	cd frontend && npm test

lint:
	cd backend && .venv/bin/ruff check .
	cd frontend && npm run lint
	cd frontend && npx tsc -b

clean:
	rm -rf $(VENV) frontend/node_modules frontend/dist

# Frees disk space by deleting old recordings/<session_key> folders. Prompts
# once per folder - never deletes without an explicit y - because a recording
# is the only artefact replay and the Phase 5 aero_raw analysis depend on.
clean-recordings:
	@if [ ! -d recordings ]; then echo "no recordings/ directory"; exit 0; fi
	@found=0; \
	for dir in recordings/*/; do \
		[ -d "$$dir" ] || continue; \
		found=1; \
		key=$$(basename "$$dir"); \
		du -sh "$$dir"; \
		printf "Delete %s? [y/N] " "$$key"; \
		read confirm; \
		case "$$confirm" in \
			y|Y) rm -rf "$$dir"; echo "deleted $$key";; \
			*) echo "kept $$key";; \
		esac; \
	done; \
	if [ "$$found" = 0 ]; then echo "no session recordings found"; fi
