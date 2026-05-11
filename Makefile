# Convenience targets so dev loops don't depend on remembering uv flags.
# `make serve` is the fast path: sets up venv if missing, installs
# deps if requirements.txt changed, runs the server. Idempotent.

.PHONY: venv install serve test lint clean

VENV ?= .venv
PY := $(VENV)/bin/python

# `--python-preference only-managed` forces uv to use its own arm64-native
# Python download (cpython-3.11.x-macos-aarch64-none) rather than the
# macOS framework Python at /Library/Frameworks/. The framework build
# is universal2 (x86_64 + arm64 fat binary), and when the launching
# shell is running under Rosetta x86_64, the framework Python inherits
# x86_64 — then arm64 wheels installed by pip fail to load with
# "incompatible architecture (have 'arm64', need 'x86_64')". A single-
# arch managed Python sidesteps that entire class of failure.
$(VENV)/.touch:
	uv python install 3.11
	uv venv --python 3.11 --python-preference only-managed $(VENV)
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
