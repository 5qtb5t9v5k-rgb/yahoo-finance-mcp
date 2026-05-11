"""HTTP-streamable MCP server exposing yfinance over MCP.

Designed to run as:
  uv run python -m yahoo_mcp.server

Speaks Streamable HTTP (the same transport Inderes MCP uses), so MCP
clients in hosted environments (Streamlit Cloud etc.) can connect via
URL — no stdio subprocess needed.

Tools shipped so far:
  - get-snapshot(ticker)  → live price, mcap, P/E, P/B, BVPS, analyst rec

Caching strategy:
  - In-memory TTL cache (15 min default) for the hot path
  - Disk-backed stale-fallback (diskcache) — when yfinance fails, we
    return the last known value with a `stale` flag instead of erroring.
    Keeps the agent flowing during yfinance breakage windows.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import diskcache
import yfinance as yf
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

CACHE_TTL_S = int(os.environ.get("YAHOO_CACHE_TTL_S", "900"))  # 15 min default
CACHE_DIR = os.environ.get("YAHOO_CACHE_DIR", "/tmp/yahoo_mcp_cache")

# In-memory hot cache (per-process) and on-disk stale fallback
_HOT_CACHE: dict[str, tuple[float, Any]] = {}
_STALE_CACHE = diskcache.Cache(CACHE_DIR, size_limit=int(1e8))  # 100 MB cap


# ─────────────────────────────────────────────────────────────────────────────
# yfinance fetch with cache + stale-fallback
# ─────────────────────────────────────────────────────────────────────────────


def _cache_get(key: str) -> dict | None:
    """Return cached info dict if still warm; None otherwise. Side effect:
    purges expired entries from the hot cache as it walks."""
    entry = _HOT_CACHE.get(key)
    if entry is None:
        return None
    cached_at, value = entry
    if time.time() - cached_at > CACHE_TTL_S:
        del _HOT_CACHE[key]
        return None
    return value


def _cache_set(key: str, value: dict) -> None:
    _HOT_CACHE[key] = (time.time(), value)
    # Also persist to disk so a process restart doesn't lose stale-fallback
    _STALE_CACHE.set(key, (time.time(), value))


def _stale_get(key: str) -> tuple[float, dict] | None:
    """Return (cached_at, value) from disk, regardless of TTL — used as
    fallback when the live fetch fails."""
    return _STALE_CACHE.get(key)


def _fetch_info(ticker: str) -> dict:
    """Live yfinance fetch. Raises on network/parsing failure — callers
    decide whether to fall back to stale cache."""
    tk = yf.Ticker(ticker)
    info = tk.info or {}
    # Smell test: Yahoo returns a stub dict for non-existent tickers.
    if info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
        raise ValueError(f"ticker {ticker!r} returned no market data")
    return info


def _get_info_cached(ticker: str) -> tuple[dict, bool]:
    """Return (info_dict, stale_flag). stale=True means yfinance fetch
    failed and we're serving the last known value from disk."""
    key = f"info:{ticker.upper()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached, False
    try:
        info = _fetch_info(ticker)
        _cache_set(key, info)
        return info, False
    except Exception as exc:  # noqa: BLE001
        log.warning("yfinance fetch failed for %s: %s", ticker, exc)
        stale = _stale_get(key)
        if stale is not None:
            _cached_at, value = stale
            return value, True
        raise


# ─────────────────────────────────────────────────────────────────────────────
# MCP server + tools
# ─────────────────────────────────────────────────────────────────────────────

mcp = FastMCP("yahoo-finance-mcp")


@mcp.tool()
def get_snapshot(ticker: str) -> dict:
    """Return a current-state snapshot for a ticker.

    Args:
        ticker: Yahoo ticker symbol. Helsinki names need the `.HE` suffix
            (e.g. ``SAMPO.HE``, ``NDA-FI.HE``). US names are bare
            (``AAPL``, ``NVDA``). European exchange suffixes:
            ``.AS`` (Amsterdam), ``.DE`` (Germany), ``.PA`` (Paris),
            ``.SW`` (Switzerland), ``.CO`` (Copenhagen), ``.ST`` (Stockholm),
            ``.OL`` (Oslo), ``.L`` (London).

    Returns:
        Dict with keys:
            ticker, name, currency, price, marketCap, trailingPE, priceToBook,
            bookValue (per-share), dividendYield, fiftyTwoWeekHigh,
            fiftyTwoWeekLow, recommendationKey, targetMeanPrice,
            numberOfAnalystOpinions, sector, industry, mostRecentQuarter,
            lastFiscalYearEnd, priceAsOf, stale.

        ``stale`` is True when yfinance is currently broken and we're
        serving a previously-cached value; agents should surface this to
        the user as a freshness disclaimer.
    """
    info, stale = _get_info_cached(ticker)
    return {
        "ticker": ticker.upper(),
        "name": info.get("longName") or info.get("shortName"),
        "currency": info.get("currency"),
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "marketCap": info.get("marketCap"),
        "trailingPE": info.get("trailingPE"),
        "priceToBook": info.get("priceToBook"),
        "bookValue": info.get("bookValue"),
        "dividendYield": info.get("dividendYield"),
        "fiftyTwoWeekHigh": info.get("fiftyTwoWeekHigh"),
        "fiftyTwoWeekLow": info.get("fiftyTwoWeekLow"),
        "recommendationKey": info.get("recommendationKey"),
        "targetMeanPrice": info.get("targetMeanPrice"),
        "numberOfAnalystOpinions": info.get("numberOfAnalystOpinions"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "mostRecentQuarter": info.get("mostRecentQuarter"),
        "lastFiscalYearEnd": info.get("lastFiscalYearEnd"),
        "priceAsOf": info.get("regularMarketTime"),
        "stale": stale,
    }


@mcp.tool()
def health() -> dict:
    """Server health + dependency check.

    Returns:
        Dict with server version, yfinance version, and a live probe of
        a known-stable ticker (AAPL). Use this from a cron to detect when
        yfinance has been broken by an upstream Yahoo change.
    """
    out = {
        "server_version": "0.1.0",
        "yfinance_version": yf.__version__,
        "probe_ticker": "AAPL",
        "probe_ok": False,
        "probe_error": None,
    }
    try:
        snap = get_snapshot("AAPL")
        out["probe_ok"] = bool(snap.get("price"))
        out["probe_stale"] = snap.get("stale", False)
    except Exception as exc:  # noqa: BLE001
        out["probe_error"] = f"{type(exc).__name__}: {exc!s}"[:200]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    # Default to streamable HTTP on port 8000. Override via MCP_HTTP_HOST /
    # MCP_HTTP_PORT env vars.
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    mcp.run(transport=transport)
