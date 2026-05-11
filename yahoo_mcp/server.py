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
def search_ticker(query: str) -> dict:
    """Resolve a company name (or partial name) to a Yahoo ticker symbol.

    For Helsinki-listed names the agent often only knows the company name
    ("Sampo", "Nokia") — this tool maps it to the Yahoo symbol
    (``SAMPO.HE``, ``NOKIA.HE``). Falls back to yfinance's lookup
    (``yf.Search``) for international names.

    Helsinki heuristic: if the bare name doesn't resolve, retry with the
    ``.HE`` suffix and a few common Finnish ticker conventions (e.g. share
    classes like ``-FI``, ``1V``).

    Args:
        query: Free-form company name. Case-insensitive.

    Returns:
        Dict with keys:
            query           — the input string echoed
            ticker          — best-match Yahoo symbol (None if no match)
            name            — long company name from Yahoo
            exchange        — exchange code (HEL, NMS, NYQ, …)
            quoteType       — EQUITY / ETF / INDEX / …
            score           — Yahoo's relevance score
            alternatives    — up to 4 other matches, each {ticker, name, exchange}
            error           — non-null when nothing was found
    """
    out: dict[str, Any] = {
        "query": query, "ticker": None, "name": None,
        "exchange": None, "quoteType": None, "score": None,
        "alternatives": [], "error": None,
    }
    try:
        # yfinance >=1.0 ships `yf.Search`. It hits Yahoo's quote-search
        # endpoint and returns dict with `quotes` list.
        results = yf.Search(query, max_results=5, news_count=0).quotes
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc!s}"[:160]
        return out

    if not results:
        # Helsinki-heuristic retry: try the bare query with `.HE` suffix,
        # then with common Finnish share-class endings.
        for suffix in (".HE", "1V.HE", "BV.HE"):
            candidate = f"{query.upper()}{suffix}"
            try:
                tk = yf.Ticker(candidate)
                info = tk.info or {}
                if info.get("regularMarketPrice") is not None:
                    out["ticker"] = candidate
                    out["name"] = info.get("longName") or info.get("shortName")
                    out["exchange"] = info.get("exchange")
                    out["quoteType"] = info.get("quoteType")
                    return out
            except Exception:
                pass
        out["error"] = "no matches found"
        return out

    primary = results[0]
    out["ticker"] = primary.get("symbol")
    out["name"] = primary.get("longname") or primary.get("shortname")
    out["exchange"] = primary.get("exchange")
    out["quoteType"] = primary.get("quoteType")
    out["score"] = primary.get("score")
    for r in results[1:]:
        out["alternatives"].append({
            "ticker": r.get("symbol"),
            "name": r.get("longname") or r.get("shortname"),
            "exchange": r.get("exchange"),
        })
    return out


@mcp.tool()
def get_history(ticker: str, period: str = "1y", interval: str = "1d") -> dict:
    """Return split-adjusted OHLCV price history for a ticker.

    Args:
        ticker: Yahoo ticker symbol.
        period: yfinance period string — ``1d``, ``5d``, ``1mo``, ``3mo``,
            ``6mo``, ``1y``, ``2y``, ``5y``, ``10y``, ``ytd``, ``max``.
            Default ``1y``.
        interval: yfinance interval string — ``1m``, ``5m``, ``15m``, ``30m``,
            ``1h``, ``1d``, ``1wk``, ``1mo``. Intraday only goes ~60 days back.
            Default ``1d``.

    Returns:
        Dict with keys:
            ticker, period, interval, rowCount,
            bars: list of {date, open, high, low, close, volume}
                  in chronological order.
            currency: from Ticker.info
            error: non-null on fetch failure.
    """
    out: dict[str, Any] = {
        "ticker": ticker.upper(), "period": period, "interval": interval,
        "rowCount": 0, "bars": [], "currency": None, "error": None,
    }
    try:
        tk = yf.Ticker(ticker)
        # auto_adjust=True applies split + dividend adjustment to OHLC.
        df = tk.history(period=period, interval=interval, auto_adjust=True)
        if df is None or df.empty:
            out["error"] = "no history data returned"
            return out
        bars = []
        for ts, row in df.iterrows():
            bars.append({
                "date": ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts, "strftime") else str(ts),
                "open": round(float(row["Open"]), 4) if row["Open"] == row["Open"] else None,
                "high": round(float(row["High"]), 4) if row["High"] == row["High"] else None,
                "low": round(float(row["Low"]), 4) if row["Low"] == row["Low"] else None,
                "close": round(float(row["Close"]), 4) if row["Close"] == row["Close"] else None,
                "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else None,
            })
        out["bars"] = bars
        out["rowCount"] = len(bars)
        try:
            out["currency"] = (tk.info or {}).get("currency")
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc!s}"[:160]
    return out


@mcp.tool()
def get_news(ticker: str, limit: int = 5) -> dict:
    """Recent news items for a ticker.

    Args:
        ticker: Yahoo ticker symbol.
        limit: Maximum items to return (default 5, hard cap 25).

    Returns:
        Dict with keys:
            ticker, count,
            items: list of {title, publisher, link, providerPublishTime, type}
            error: non-null on fetch failure.
    """
    out: dict[str, Any] = {"ticker": ticker.upper(), "count": 0, "items": [], "error": None}
    limit = max(1, min(int(limit or 5), 25))
    try:
        tk = yf.Ticker(ticker)
        news = tk.news or []
        for n in news[:limit]:
            # yfinance returns news with nested `content` key (recent API change).
            content = n.get("content") if isinstance(n.get("content"), dict) else n
            out["items"].append({
                "title": content.get("title"),
                "publisher": (content.get("provider") or {}).get("displayName")
                             if isinstance(content.get("provider"), dict)
                             else content.get("publisher"),
                "link": (content.get("canonicalUrl") or {}).get("url")
                        if isinstance(content.get("canonicalUrl"), dict)
                        else content.get("link"),
                "providerPublishTime": content.get("pubDate") or content.get("providerPublishTime"),
                "type": content.get("contentType") or content.get("type"),
            })
        out["count"] = len(out["items"])
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc!s}"[:160]
    return out


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
def get_holders(ticker: str) -> dict:
    """Institutional + mutual fund + insider ownership for a ticker.

    The Bloomberg HDS-equivalent. Useful for "who's positioned in this
    name" questions where Inderes' Finnish insider data alone is not
    enough — e.g. when researching US/EU names where institutional
    holdings are a stronger signal than retail forum chatter.

    Args:
        ticker: Yahoo ticker symbol.

    Returns:
        Dict with keys:
            ticker,
            major_holders: top-line breakdown {insiders_pct,
                           institutions_pct, institutions_float_pct,
                           num_institutions},
            top_institutions: list of {holder, shares, dateReported,
                              pctOut, value}
            top_funds: same shape, mutual-fund holders
            insider_transactions: list of recent insider tx
                                  {insider, position, transaction,
                                   shares, value, date}
            error: non-null on fetch failure.
    """
    out: dict[str, Any] = {
        "ticker": ticker.upper(),
        "major_holders": {},
        "top_institutions": [],
        "top_funds": [],
        "insider_transactions": [],
        "section_errors": {},
        "error": None,
    }
    try:
        tk = yf.Ticker(ticker)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc!s}"[:160]
        return out

    # Major-holders breakdown — yfinance returns a small DataFrame with
    # the breakdown labels as the index and a single ``Value`` column.
    try:
        mh = tk.major_holders
        if mh is not None and not mh.empty:
            if "Value" in mh.columns:
                for label, row in mh.iterrows():
                    key = str(label).lower().replace(" ", "_")
                    val = row["Value"]
                    # Coerce numpy scalars to Python floats so the JSON
                    # response is portable across MCP clients.
                    try:
                        val = float(val) if val == val else None
                    except Exception:
                        val = str(val)
                    out["major_holders"][key] = val
            else:
                # Older positional shape (yfinance < 0.2.40)
                for i, row in mh.iterrows():
                    out["major_holders"][f"row_{i}"] = [str(x) for x in row]
    except Exception as exc:  # noqa: BLE001
        out["section_errors"]["major_holders"] = f"{type(exc).__name__}: {exc!s}"[:160]

    # Top institutional holders
    try:
        inst = tk.institutional_holders
        if inst is not None and not inst.empty:
            for _, row in inst.head(10).iterrows():
                out["top_institutions"].append({
                    "holder": row.get("Holder"),
                    "shares": int(row["Shares"]) if "Shares" in row and row["Shares"] == row["Shares"] else None,
                    "dateReported": str(row.get("Date Reported", "")),
                    "pctOut": float(row["pctHeld"]) if "pctHeld" in row and row["pctHeld"] == row["pctHeld"] else None,
                    "value": float(row["Value"]) if "Value" in row and row["Value"] == row["Value"] else None,
                })
    except Exception as exc:  # noqa: BLE001
        out["section_errors"]["institutional_holders"] = f"{type(exc).__name__}: {exc!s}"[:160]

    # Top mutual-fund holders
    try:
        funds = tk.mutualfund_holders
        if funds is not None and not funds.empty:
            for _, row in funds.head(10).iterrows():
                out["top_funds"].append({
                    "holder": row.get("Holder"),
                    "shares": int(row["Shares"]) if "Shares" in row and row["Shares"] == row["Shares"] else None,
                    "dateReported": str(row.get("Date Reported", "")),
                    "pctOut": float(row["pctHeld"]) if "pctHeld" in row and row["pctHeld"] == row["pctHeld"] else None,
                    "value": float(row["Value"]) if "Value" in row and row["Value"] == row["Value"] else None,
                })
    except Exception as exc:  # noqa: BLE001
        out["section_errors"]["mutualfund_holders"] = f"{type(exc).__name__}: {exc!s}"[:160]

    # Recent insider transactions
    try:
        it = tk.insider_transactions
        if it is not None and not it.empty:
            for _, row in it.head(15).iterrows():
                out["insider_transactions"].append({
                    "insider": row.get("Insider"),
                    "position": row.get("Position"),
                    "transaction": row.get("Transaction"),
                    "shares": int(row["Shares"]) if "Shares" in row and row["Shares"] == row["Shares"] else None,
                    "value": float(row["Value"]) if "Value" in row and row["Value"] == row["Value"] else None,
                    "date": str(row.get("Start Date", "")),
                })
    except Exception as exc:  # noqa: BLE001
        out["section_errors"]["insider_transactions"] = f"{type(exc).__name__}: {exc!s}"[:160]

    # If every section failed AND we got nothing, promote to top-level error
    # so callers can branch on a single field.
    if (
        not out["major_holders"]
        and not out["top_institutions"]
        and not out["top_funds"]
        and not out["insider_transactions"]
        and out["section_errors"]
    ):
        # Pick the first rate-limit-looking error for the top-level summary
        first_err = next(iter(out["section_errors"].values()))
        out["error"] = first_err
    return out


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
