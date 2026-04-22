# Velox Lite

**Goal:** Run Velox on Alpaca + Anthropic + free feeds only. Target monthly burn: under $50.

## What Lite mode disables

| Provider | Monthly burn at peak | Disabled in Lite? | Re-enable flag |
|---|---|---|---|
| X / Twitter API | ~$1,400 | YES | `X_API_ENABLED=true` |
| Polygon | $199 | YES | `POLYGON_API_ENABLED=true` |
| Unusual Whales | $375 | YES | `UW_API_ENABLED=true` |
| xAI / Grok | ~$5-15/day | YES | `XAI_API_ENABLED=true` |
| OpenAI | ~$5-10/day | YES | `OPENAI_API_ENABLED=true` |
| Perplexity | ~$2-5/day | YES | `PERPLEXITY_API_ENABLED=true` |
| **Kept: Alpaca** | free (paper) | NO | - |
| **Kept: Anthropic** | ~$5/day | NO | - |
| **Kept: FRED / Finnhub / StockTwits / EDGAR** | free | NO | - |

## The master switch

`VELOX_LITE=true` disables every paid provider at init, regardless of whether keys are present in `.env`. Code for each provider remains in the repo — nothing deleted. When you want a provider back, flip its `_ENABLED` flag to `true` and restart. Per-provider flags override `VELOX_LITE`.

## What changes in the bot

- **Scanner:** loses Polygon gainers/losers, UW flow, Grok X trending, and copy-trader signals. Keeps Alpaca most-actives, watchlist, StockTwits trending, pharma catalysts, EDGAR filings, earnings calendar, fade runner scans.
- **Sentiment:** Twitter path and Perplexity path both short-circuit to neutral. VADER keyword scoring on whatever free text is available.
- **Jury / Consensus:** Claude-only (GPT and Grok return `None` when disabled). Single-model vote.
- **Market data:** Polygon REST is a no-op. `get_price()` routes through Alpaca automatically. Historical bars and gainers/losers return empty lists — the scanner already handles this path.

## Re-enabling one provider at a time

When exitFi starts producing revenue and you want Polygon back:

```
POLYGON_API_KEY=<your key>
POLYGON_API_ENABLED=true
# Leave VELOX_LITE=true — per-provider flag wins
```

Restart. Polygon is back. Everything else stays cold.

## VPS redeploy (DigitalOcean Droplet)

Host: `root@174.138.81.55`, path `/opt/velox-app`, service `velox.service`.

```bash
ssh root@174.138.81.55
cd /opt/velox-app

# Pull the Lite branch
git fetch origin
git checkout velox-lite
git pull

# Swap the env. Two options:
#  A) Keep current .env and just append the kill switches (minimum-risk):
#     echo 'VELOX_LITE=true' >> .env
#  B) Start clean from the template and paste your real keys back in:
#     cp .env .env.backup.$(date +%Y%m%d)
#     cp .env.lite.example .env
#     nano .env   # paste ALPACA_*, ANTHROPIC_API_KEY, FRED/FINNHUB/STOCKTWITS from backup

# Restart the service
systemctl restart velox

# Tail the logs — you should see the 🪶 banners within 30s
journalctl -u velox -f
```

You should see within 30 seconds:

```
🪶 VELOX_LITE=true — all paid/external APIs disabled...
🪶 X API disabled — twitter_client + copy_trader_monitor skipped
🪶 Unusual Whales disabled — uw_client, options_scanner, congress_scanner, uw_stream skipped
🪶 xAI disabled — grok_x_trending skipped
All components initialized
```

If you see any unguarded paid-API call (`api.twitter.com`, `unusualwhales.com`, `api.x.ai`, `api.openai.com`, `api.perplexity.ai`, `api.polygon.io`) in the logs, that's a bug to fix — open an issue with the log excerpt.

## Kill-switch stop (panic mode)

If you need to kill the bot immediately without redeploying:

```bash
ssh root@174.138.81.55
systemctl stop velox
systemctl disable velox     # prevent restart on reboot
```

To bring it back:

```bash
systemctl enable velox
systemctl start velox
```

## Graduation back to full Velox

Velox Lite is not the endgame. It's a cost-controlled proving ground. The graduation criteria to re-enable the expensive desks:

1. **Lite has 30 consecutive trading days with net-positive paper P&L**, OR
2. **exitFi produces sufficient MRR to cover the data bill**, OR
3. **You have a specific hypothesis** that requires a specific paid feed, not a general "more signals = better"

When any one of those is true, flip a single `_ENABLED` flag, measure the incremental P&L contribution of that feed over 30 days, and only then consider adding the next one. Every feed earns its budget.
