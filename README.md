# yahoo-finance-mcp

**HTTP-streamable MCP server wrapping [`yfinance`](https://github.com/ranaroussi/yfinance).**

Fills a gap in the MCP ecosystem: as of mid-2026, every public
Yahoo-Finance MCP server is **stdio-only**. This one speaks **streamable
HTTP**, which is what hosted agent platforms (Streamlit Cloud, etc.)
need when running MCP clients that can't spawn subprocesses.

Designed for personal-use investment research over both Finnish
(Helsinki `.HE`) and international tickers. Built originally for the
[Inderes MCP agent](https://github.com/5qtb5t9v5k-rgb/inderes-mcp-agent-system),
where it complements the Finnish-coverage Inderes MCP with live price
+ quarterly book value + global ticker support.

## Status

✅ **5 tools shipped** (snapshot, history, news, holders, search) + health probe.
Modal deploy config is the next milestone. License MIT.

## Architecture

```
┌───────────────────────────────────────────┐
│  Your agent (MCP client)                   │
│                                            │
│   ┌─────────────────────────────────────┐  │
│   │ MCPStreamableHTTPTool               │  │
│   │ url=https://yahoo-mcp.example.com/  │  │
│   └─────────────────────────────────────┘  │
└────────────────────┬───────────────────────┘
                     │ HTTP (streamable)
                     ▼
┌───────────────────────────────────────────┐
│  yahoo-finance-mcp server                  │
│  • FastMCP + streamable_http transport     │
│  • yfinance 1.3.0 (pinned)                 │
│  • curl_cffi (TLS-fingerprint shim)        │
│  • In-memory TTL cache (15 min)            │
│  • Disk stale-fallback for outages         │
└────────────────────┬───────────────────────┘
                     │ HTTPS (impersonating browser)
                     ▼
              Yahoo Finance
```

## Tools

| Tool | Description |
|---|---|
| `search_ticker(query)` | Resolve company name → ticker (with Helsinki `.HE` heuristic) |
| `get_snapshot(ticker)` | Live price + market cap + P/E + P/B + bookValue + analyst consensus + freshness flag |
| `get_history(ticker, period, interval)` | Split- and dividend-adjusted OHLCV bars |
| `get_news(ticker, limit)` | Recent news items (supports the old flat + new nested yfinance shapes) |
| `get_holders(ticker)` | Major holders %, top institutions, top mutual funds, recent insider transactions |
| `health()` | Server version + yfinance version + live AAPL probe — cron-ready |

## Why not just use yfinance directly?

Three reasons baked into the existing project's `LESSONS.md`:

1. **Isolation.** yfinance breaks when Yahoo changes its frontend (multiple
   times per year). Wrapping it in an MCP server localises the breakage to
   one component instead of letting it cascade into every agent.
2. **Per-agent tool partitioning.** The host agent already enforces
   per-domain tool ownership (QUANT vs RESEARCH vs SENTIMENT etc.). Having
   Yahoo behind an MCP keeps that pattern; an in-process Python library
   would smear across agents.
3. **Same connection pattern as Inderes MCP.** The host project's
   sanitizing-tool wrapper, fabrication guard, hard limits, and
   per-claim provenance markers all apply unchanged to anything served
   via MCP.

## Running locally

```bash
# One-shot setup + run (uses uv)
make serve

# …or manually:
uv venv --python 3.11
uv pip install -r requirements.txt
.venv/bin/python -m yahoo_mcp.server
```

Server listens on `http://localhost:8000/mcp`. Set
`YAHOO_MCP_URL=http://localhost:8000/mcp` in your client to point an
agent at it.

**Apple Silicon note:** if you see
`ImportError: ... incompatible architecture (have 'arm64', need 'x86_64')`,
the macOS framework Python at `/Library/Frameworks/Python.framework/`
is a *universal2 binary* (fat x86_64 + arm64). When it's launched from
a shell that's running under Rosetta, it inherits x86_64 — but pip
still installs arm64 wheels, and they can't load. The Makefile passes
`--python-preference only-managed` to `uv venv` so the venv uses uv's
own single-architecture arm64 Python (`cpython-3.11.x-macos-aarch64`)
instead of the framework build. That avoids the whole mess.

If you set up manually with `uv venv --python 3.11 .venv`, the venv
will silently inherit the framework Python's arch. Add
`--python-preference only-managed` and run `uv python install 3.11`
first to get the arm64-only build.

## Testing

```bash
# Offline suite (yfinance fully mocked, no network)
python -m pytest tests/ -v

# Optional live probe — hits Yahoo for real
YAHOO_MCP_LIVE=1 python -m pytest tests/ -v -m live
```

The CI workflow runs the offline suite on every PR and the live probe
once a day on a schedule — that gives us a canary for "Yahoo broke
yfinance" without making PRs depend on Yahoo's availability.

## Hosting

Designed for [Modal](https://modal.com) free tier. Deploy:

```bash
modal deploy yahoo_mcp/modal_app.py   # 🚧 todo
```

## License

MIT — use freely. Contributions welcome, especially:
- Ticker-resolution heuristics for additional non-US exchanges
- Cache backend pluggability (Redis, etc.)
- Health-check endpoint that pings yfinance regression-style

## Related

- [`ranaroussi/yfinance`](https://github.com/ranaroussi/yfinance) — the underlying scraper
- [Inderes MCP agent](https://github.com/5qtb5t9v5k-rgb/inderes-mcp-agent-system) — the project this was built for
- [`Alex2Yang97/yahoo-finance-mcp`](https://github.com/Alex2Yang97/yahoo-finance-mcp) — same idea but stdio-only (no HTTP transport)
