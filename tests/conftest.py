"""Shared pytest fixtures.

We isolate the diskcache per-test so cached values from one test don't
bleed into the next, and we patch ``yf.Ticker`` to avoid hitting Yahoo
in the default test run.
"""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the disk cache at a fresh tempdir per test so cache state
    can't leak across tests."""
    tmpdir = tempfile.mkdtemp(prefix="yahoo_mcp_test_")
    monkeypatch.setenv("YAHOO_CACHE_DIR", tmpdir)
    # Reset the module-level cache singletons. yahoo_mcp.server is loaded
    # once per process and stores the diskcache handle in a module global,
    # so we have to re-import after setting the env var.
    import importlib

    import yahoo_mcp.server as srv

    importlib.reload(srv)


@pytest.fixture
def fake_info() -> dict:
    """Minimal info dict that mimics yfinance's ``Ticker.info`` shape for
    SAMPO.HE. Use this when you want a ticker to ``look healthy``
    (i.e. ``regularMarketPrice`` is set so the smell-test passes)."""
    return {
        "longName": "Sampo Oyj",
        "shortName": "Sampo",
        "currency": "EUR",
        "currentPrice": 9.42,
        "regularMarketPrice": 9.42,
        "marketCap": 24_500_000_000,
        "trailingPE": 14.2,
        "priceToBook": 1.8,
        "bookValue": 5.2,
        "dividendYield": 0.06,
        "fiftyTwoWeekHigh": 10.1,
        "fiftyTwoWeekLow": 7.4,
        "recommendationKey": "buy",
        "targetMeanPrice": 10.5,
        "numberOfAnalystOpinions": 12,
        "sector": "Financial Services",
        "industry": "Insurance—Diversified",
        "mostRecentQuarter": 1727654400,
        "lastFiscalYearEnd": 1735603200,
        "regularMarketTime": 1747400000,
        "exchange": "HEL",
        "quoteType": "EQUITY",
    }


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``@pytest.mark.live`` tests unless ``YAHOO_MCP_LIVE=1``.

    Live tests hit Yahoo and are inherently flaky (rate limits, regional
    blocks). Keep CI deterministic by gating them behind an env var.
    """
    if os.environ.get("YAHOO_MCP_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason="set YAHOO_MCP_LIVE=1 to run live network tests")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
