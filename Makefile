# Convenience targets so dev loops don't depend on remembering uv flags.
# `make serve` is the fast path: sets up venv if missing, installs
# deps if requirements.txt changed, runs the server. Idempotent.

.PHONY: venv install serve test lint clean

VENV ?= .venv
PY := $(VENV)/bin/python

$(VENV)/.touch:
	uv venv --python 3.11 $(VENV)
	@touch $(VENV)/.touch

venv: $(VENV)/.touch

$(VENV)/.deps: $(VENV)/.touch requirements.txt
	VIRTUAL_ENV=$(VENV) uv pip install -r requirements.txt
	@touch $(VENV)/.deps

install: $(VENV)/.deps

# Run the MCP server on localhost:8000.
serve: install
	$(PY) -m yahoo_mcp.server

# Offline tests (yfinance fully mocked).
test: install
	VIRTUAL_ENV=$(VENV) uv pip install pytest pandas
	$(PY) -m pytest tests/ -v

# Opt-in live tests — hit Yahoo for real.
test-live: install
	VIRTUAL_ENV=$(VENV) uv pip install pytest pandas
	YAHOO_MCP_LIVE=1 $(PY) -m pytest tests/ -v -m live

lint: install
	VIRTUAL_ENV=$(VENV) uv pip install ruff==0.14.0
	$(PY) -m ruff check yahoo_mcp tests

clean:
	rm -rf $(VENV) .pytest_cache yahoo_mcp/__pycache__ tests/__pycache__
