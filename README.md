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

🚧 Early — working snapshot tool, others in progress. License MIT.

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

## Tools (planned)

| Tool | Status | Description |
|---|---|---|
| `get-snapshot` | ✅ shipped | Live price + market cap + P/E + P/B + bookValue + analyst consensus |
| `get-history` | 🚧 | Split-adjusted OHLC price history |
| `search-ticker` | 🚧 | Resolve company name → ticker (with Helsinki `.HE` heuristics) |
| `get-news` | 💭 | Recent news items for ticker |
| `get-holders` | 💭 | Institutional + insider holdings |

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
# Setup
uv pip install -r requirements.txt

# Run on localhost:8000
uv run python -m yahoo_mcp.server
```

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
