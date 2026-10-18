# toss-invest-mcp

An **unofficial MCP server** for the [Toss Invest (토스증권) Open API](https://developers.tossinvest.com), for Claude, Cursor and other MCP clients.
Quotes, candles, technical indicators and account data are read freely; **orders go through multiple guard rails**.

[한국어](README.md)

> ⚠️ Not affiliated with Toss Invest. This tool moves real money — use the default `dry_run` mode until you trust it.
> Provided under the MIT license with no warranty.

## Tools

| Group | Tools |
|---|---|
| Market data | `quote`, `orderbook`, `candles` (1d/1m), `stock_info` |
| Indicators | `technicals` — RSI(14, Wilder), Bollinger(20, 2σ), SMA 5/20/60, % change, streaks |
| Market | `market_hours_us`, `kr_market` (KOSPI/KOSDAQ/KR bonds), `fx_usdkrw` |
| Account | `holdings`, `buying_power`, `orders`, `order_detail` |
| Trading | `preview_order` → `place_order`, `cancel_order`, `trading_status`, `order_log` |

Korean (6-digit codes) and US (tickers) stocks. Limit, market, **LOC (limit-on-close)**, US notional orders and fractional sells.

## Guard rails

1. **Two-step orders** — `preview_order` validates limits, cash and sellable quantity and returns a `preview_id`; `place_order` executes it only after the user confirms. Previews expire (5 min) and are single-use.
2. **Modes** — `off` / `dry_run` (default) / `live`.
3. **Buy limits (optional)** — unlimited by default; set them yourself via env vars: per-order and daily notional caps (USD and KRW separately) and a daily buy count. Market orders are estimated with a 3% buffer. Sells are exempt so a stop-loss is never blocked (sellable quantity is still checked). The model cannot change limits.
4. **Symbol allow-list** — `ALLOWED_SYMBOLS`.
5. **Kill switch** — `toss-invest-mcp kill on` blocks new orders instantly, no restart needed. Cancels still work.
6. **Out-of-band confirmation (optional)** — with `NTFY_TOPIC`, a 6-digit code is pushed to your phone via [ntfy](https://ntfy.sh). The model never sees it, so a human has to type it in.
7. **Idempotency** — `preview_id` is sent as `clientOrderId`, so retries never double-order.
8. Warnings for leveraged/inverse ETFs and far-from-market limit prices.
9. Every preview, execution and rejection is logged to `orders.log.jsonl`.

## Setup

1. Issue API keys at the Toss Invest Open API console.
2. Register your machine's public IP under WTS > Settings > Open API > **Allowed IPs**.
3. Install Python 3.10+ and [uv](https://docs.astral.sh/uv/).

Toss allows only one valid token per client. The server caches it in `DATA_DIR/token.json` and shares it between processes.

### Claude Desktop / Cursor (stdio)

```json
{
  "mcpServers": {
    "toss-invest": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/jihwan0102/toss-invest-mcp", "toss-invest-mcp"],
      "env": { "TOSS_CLIENT_ID": "...", "TOSS_CLIENT_SECRET": "...", "TRADING_MODE": "dry_run" }
    }
  }
}
```

### Claude Code

```bash
claude mcp add toss-invest -e TOSS_CLIENT_ID=... -e TOSS_CLIENT_SECRET=... \
  -- uvx --from git+https://github.com/jihwan0102/toss-invest-mcp toss-invest-mcp
```

### Remote HTTP (e.g. claude.ai custom connector)

```bash
uv tool install git+https://github.com/jihwan0102/toss-invest-mcp
cp .env.example toss.env && chmod 600 toss.env
echo "MCP_PATH_SECRET=$(toss-invest-mcp gen-secret)" >> toss.env
toss-invest-mcp --env-file toss.env serve --http
```

Put it behind an HTTPS reverse proxy and register `https://<host>/<MCP_PATH_SECRET>/mcp`.
`MCP_AUTH_TOKEN` enables Bearer auth for clients that can send headers. See [`deploy/`](deploy/) for systemd and nginx examples.

## CLI

```bash
toss-invest-mcp check | status | kill on|off | logs -n 50 | gen-secret
```

All configuration is via environment variables — see the table in the [Korean README](README.md#설정-환경변수) or [`.env.example`](.env.example).

## Development

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest && .venv/bin/ruff check .
```

Tests use `httpx.MockTransport`; no API keys needed.

## License

MIT
