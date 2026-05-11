"""Smoke + behavior tests for yahoo_mcp.server.

The default suite is fully offline — yfinance is patched. The handful of
``@pytest.mark.live`` tests at the bottom hit Yahoo for real; gate them
with ``YAHOO_MCP_LIVE=1`` when you want to verify upstream still works.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _mock_ticker(info: dict | None = None, history: pd.DataFrame | None = None,
                 news: list | None = None, major_holders: pd.DataFrame | None = None,
                 institutional: pd.DataFrame | None = None,
                 mutualfund: pd.DataFrame | None = None,
                 insider_tx: pd.DataFrame | None = None) -> MagicMock:
    """Return a MagicMock that mimics ``yf.Ticker`` with the given data."""
    tk = MagicMock()
    tk.info = info or {}
    tk.history = MagicMock(return_value=history if history is not None else pd.DataFrame())
    tk.news = news or []
    tk.major_holders = major_holders if major_holders is not None else pd.DataFrame()
    tk.institutional_holders = institutional if institutional is not None else pd.DataFrame()
    tk.mutualfund_holders = mutualfund if mutualfund is not None else pd.DataFrame()
    tk.insider_transactions = insider_tx if insider_tx is not None else pd.DataFrame()
    return tk


# ─────────────────────────────────────────────────────────────────────────────
# get_snapshot
# ─────────────────────────────────────────────────────────────────────────────


def test_snapshot_returns_normalized_dict(fake_info):
    from yahoo_mcp import server

    with patch.object(server.yf, "Ticker", return_value=_mock_ticker(info=fake_info)):
        snap = server.get_snapshot("SAMPO.HE")

    assert snap["ticker"] == "SAMPO.HE"
    assert snap["name"] == "Sampo Oyj"
    assert snap["currency"] == "EUR"
    assert snap["price"] == 9.42
    assert snap["trailingPE"] == 14.2
    assert snap["stale"] is False


def test_snapshot_falls_back_to_stale_cache_on_fetch_error(fake_info):
    """When yfinance breaks, we should serve the last known value rather
    than raising. That's the entire reason the disk cache exists."""
    from yahoo_mcp import server

    # First call: succeeds and populates both hot + stale caches.
    with patch.object(server.yf, "Ticker", return_value=_mock_ticker(info=fake_info)):
        first = server.get_snapshot("SAMPO.HE")
    assert first["stale"] is False
    assert first["price"] == 9.42

    # Bust the hot cache to force the fallback path.
    server._HOT_CACHE.clear()

    # Second call: yfinance raises → expect stale-cache fallback.
    bad_ticker = MagicMock()
    bad_ticker.info = MagicMock(side_effect=RuntimeError("yahoo broken"))
    type(bad_ticker).info = property(lambda _: (_ for _ in ()).throw(RuntimeError("yahoo broken")))

    with patch.object(server.yf, "Ticker", return_value=bad_ticker):
        second = server.get_snapshot("SAMPO.HE")

    assert second["stale"] is True
    assert second["price"] == 9.42  # Same value as the warm call


def test_snapshot_smell_test_rejects_empty_info():
    """Yahoo returns a stub dict for non-existent tickers — the smell-test
    in ``_fetch_info`` should catch that and raise so we don't hand the
    agent a snapshot full of Nones."""
    from yahoo_mcp import server

    empty_ticker = MagicMock()
    empty_ticker.info = {}  # No regularMarketPrice / currentPrice

    with patch.object(server.yf, "Ticker", return_value=empty_ticker), pytest.raises(ValueError):
        server._fetch_info("ZZZZ.NONEXISTENT")


# ─────────────────────────────────────────────────────────────────────────────
# search_ticker
# ─────────────────────────────────────────────────────────────────────────────


def test_search_ticker_returns_primary_match():
    from yahoo_mcp import server

    fake_search = MagicMock()
    fake_search.quotes = [
        {"symbol": "AAPL", "longname": "Apple Inc.", "exchange": "NMS",
         "quoteType": "EQUITY", "score": 100_000},
        {"symbol": "APLE", "longname": "Apple Hospitality REIT", "exchange": "NYQ",
         "quoteType": "EQUITY"},
    ]
    with patch.object(server.yf, "Search", return_value=fake_search):
        out = server.search_ticker("Apple")

    assert out["ticker"] == "AAPL"
    assert out["name"] == "Apple Inc."
    assert len(out["alternatives"]) == 1
    assert out["alternatives"][0]["ticker"] == "APLE"
    assert out["error"] is None


def test_search_ticker_helsinki_heuristic_when_search_empty():
    """If yfinance Search returns nothing, the tool should retry with
    ``.HE`` suffix — the most common case for Helsinki-listed names the
    LLM only knows by base ticker symbol."""
    from yahoo_mcp import server

    fake_search = MagicMock()
    fake_search.quotes = []

    sampo_info = {
        "regularMarketPrice": 9.42,
        "longName": "Sampo Oyj",
        "exchange": "HEL",
        "quoteType": "EQUITY",
    }

    with patch.object(server.yf, "Search", return_value=fake_search), \
         patch.object(server.yf, "Ticker", return_value=_mock_ticker(info=sampo_info)):
        out = server.search_ticker("SAMPO")

    assert out["ticker"] == "SAMPO.HE"
    assert out["name"] == "Sampo Oyj"


def test_search_ticker_handles_search_exception_gracefully():
    from yahoo_mcp import server

    with patch.object(server.yf, "Search", side_effect=RuntimeError("Yahoo down")):
        out = server.search_ticker("Anything")

    assert out["ticker"] is None
    assert out["error"] is not None
    assert "RuntimeError" in out["error"]


# ─────────────────────────────────────────────────────────────────────────────
# get_history
# ─────────────────────────────────────────────────────────────────────────────


def test_history_returns_bars_in_chronological_order():
    from yahoo_mcp import server

    df = pd.DataFrame(
        {
            "Open": [10.0, 10.5, 11.0],
            "High": [10.5, 11.0, 11.5],
            "Low": [9.8, 10.2, 10.7],
            "Close": [10.4, 10.9, 11.3],
            "Volume": [1000, 1100, 1200],
        },
        index=pd.to_datetime(["2026-05-01", "2026-05-02", "2026-05-03"]),
    )
    ticker = _mock_ticker(info={"currency": "EUR"}, history=df)

    with patch.object(server.yf, "Ticker", return_value=ticker):
        out = server.get_history("SAMPO.HE", period="5d", interval="1d")

    assert out["rowCount"] == 3
    assert out["currency"] == "EUR"
    assert out["bars"][0]["date"].startswith("2026-05-01")
    assert out["bars"][-1]["close"] == 11.3
    assert out["error"] is None


def test_history_empty_dataframe_returns_no_data_error():
    from yahoo_mcp import server

    ticker = _mock_ticker(history=pd.DataFrame())
    with patch.object(server.yf, "Ticker", return_value=ticker):
        out = server.get_history("ZZZZ", period="1mo")

    assert out["rowCount"] == 0
    assert out["error"] == "no history data returned"


# ─────────────────────────────────────────────────────────────────────────────
# get_news
# ─────────────────────────────────────────────────────────────────────────────


def test_news_parses_modern_nested_content_shape():
    """yfinance recently moved its news payload under a nested ``content``
    key. The parser should support both the old flat shape and the new
    nested shape."""
    from yahoo_mcp import server

    nested = [
        {
            "id": "uuid-1",
            "content": {
                "title": "Sampo lifts guidance",
                "provider": {"displayName": "Reuters"},
                "canonicalUrl": {"url": "https://example.com/sampo-1"},
                "pubDate": "2026-05-09T08:00:00Z",
                "contentType": "STORY",
            },
        },
        {
            # Old flat shape for backward-compat
            "title": "Apple beats estimates",
            "publisher": "Bloomberg",
            "link": "https://example.com/aapl-1",
            "providerPublishTime": 1747000000,
            "type": "STORY",
        },
    ]
    ticker = _mock_ticker(news=nested)

    with patch.object(server.yf, "Ticker", return_value=ticker):
        out = server.get_news("MIXED", limit=5)

    assert out["count"] == 2
    assert out["items"][0]["title"] == "Sampo lifts guidance"
    assert out["items"][0]["publisher"] == "Reuters"
    assert out["items"][0]["link"] == "https://example.com/sampo-1"
    assert out["items"][1]["title"] == "Apple beats estimates"
    assert out["items"][1]["publisher"] == "Bloomberg"


def test_news_limit_is_clamped_to_25():
    from yahoo_mcp import server

    fake_news = [{"content": {"title": f"item-{i}"}} for i in range(40)]
    ticker = _mock_ticker(news=fake_news)

    with patch.object(server.yf, "Ticker", return_value=ticker):
        out = server.get_news("X", limit=100)

    assert out["count"] == 25  # hard cap


# ─────────────────────────────────────────────────────────────────────────────
# get_holders
# ─────────────────────────────────────────────────────────────────────────────


def test_holders_parses_major_breakdown_into_flat_dict():
    from yahoo_mcp import server

    mh = pd.DataFrame(
        {"Value": [0.0164, 0.6758, 0.6870, 7572.0]},
        index=["insidersPercentHeld", "institutionsPercentHeld",
               "institutionsFloatPercentHeld", "institutionsCount"],
    )
    inst = pd.DataFrame([
        {"Date Reported": "2025-12-31", "Holder": "Blackrock Inc.",
         "pctHeld": 0.072, "Shares": 1_120_000_000, "Value": 337_000_000_000.0,
         "pctChange": 0.007},
        {"Date Reported": "2026-03-31", "Holder": "Vanguard",
         "pctHeld": 0.068, "Shares": 1_050_000_000, "Value": 278_000_000_000.0,
         "pctChange": 1.0},
    ])
    insider_tx = pd.DataFrame([
        {"Shares": 5000, "Value": 0.0, "URL": "", "Text": "",
         "Insider": "John Smith", "Position": "Director", "Transaction": "Sale",
         "Start Date": "2026-05-06", "Ownership": "D"},
    ])
    ticker = _mock_ticker(major_holders=mh, institutional=inst, insider_tx=insider_tx)

    with patch.object(server.yf, "Ticker", return_value=ticker):
        out = server.get_holders("AAPL")

    assert out["major_holders"]["insiderspercentheld"] == pytest.approx(0.0164)
    assert out["major_holders"]["institutionscount"] == pytest.approx(7572.0)
    assert len(out["top_institutions"]) == 2
    assert out["top_institutions"][0]["holder"] == "Blackrock Inc."
    assert out["top_institutions"][0]["pctOut"] == pytest.approx(0.072)
    assert len(out["insider_transactions"]) == 1
    assert out["insider_transactions"][0]["insider"] == "John Smith"
    assert out["error"] is None


def test_holders_returns_top_level_error_when_all_sections_fail():
    """If every yfinance holder endpoint blows up (rate limit, network,
    upstream API change), the tool should surface a top-level ``error``
    so the agent can branch on a single field rather than four section
    error keys."""
    from yahoo_mcp import server

    rate_limited = MagicMock()
    rate_limit_exc = RuntimeError("Too Many Requests")
    type(rate_limited).major_holders = property(lambda _: (_ for _ in ()).throw(rate_limit_exc))
    type(rate_limited).institutional_holders = property(lambda _: (_ for _ in ()).throw(rate_limit_exc))
    type(rate_limited).mutualfund_holders = property(lambda _: (_ for _ in ()).throw(rate_limit_exc))
    type(rate_limited).insider_transactions = property(lambda _: (_ for _ in ()).throw(rate_limit_exc))

    with patch.object(server.yf, "Ticker", return_value=rate_limited):
        out = server.get_holders("AAPL")

    assert out["error"] is not None
    assert "Too Many Requests" in out["error"]
    assert len(out["section_errors"]) == 4


# ─────────────────────────────────────────────────────────────────────────────
# health
# ─────────────────────────────────────────────────────────────────────────────


def test_health_reports_yfinance_version_and_probe(fake_info):
    from yahoo_mcp import server

    with patch.object(server.yf, "Ticker", return_value=_mock_ticker(info=fake_info)):
        out = server.health()

    assert out["server_version"] == "0.1.0"
    assert out["yfinance_version"]
    assert out["probe_ticker"] == "AAPL"
    assert out["probe_ok"] is True
    assert out["probe_error"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Live integration tests — opt-in via YAHOO_MCP_LIVE=1
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.live
def test_live_snapshot_sampo():
    """Real Yahoo fetch — verifies upstream hasn't drifted."""
    from yahoo_mcp import server

    snap = server.get_snapshot("SAMPO.HE")
    assert snap["currency"] == "EUR"
    assert snap["price"] is not None
    assert snap["sector"]  # Sector should always be set for a real ticker


@pytest.mark.live
def test_live_search_apple():
    from yahoo_mcp import server

    out = server.search_ticker("Apple")
    assert out["ticker"] in ("AAPL", "AAPL.NMS", "AAPL.NEO")
    assert "Apple" in (out["name"] or "")
