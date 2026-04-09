# GMC Morning Brief

A daily intelligence digest for Grist Mill Capital — delivered automatically at 5:30 AM CT every trading day.

## What It Does

Pulls from multiple free data sources, summarizes everything with Claude AI, and emails a clean HTML digest to your inbox before the market opens.

**Daily email (5:30 AM CT, Mon–Fri):**
1. **Macro Calendar** — Today's scheduled US economic releases with estimates and actuals
2. **Yesterday's Numbers** — Prior day actuals vs estimates
3. **This Day in History** — 3 curated historical events, finance-weighted
4. **Position Intelligence** — Claude-summarized news for every open equity and crypto position
5. **Inbox Highlights** — Relevant financial newsletters and scanner alerts from Gmail

**Weekly close email (3:30 PM CT, Friday):**
- Week in Review macro summary
- Full week's macro releases
- Weekly position digest

## Data Sources

| Source | What It Provides | Cost |
|--------|-----------------|------|
| ForexFactory JSON | US macro calendar (GDP, CPI, jobs, Fed speakers) | Free |
| Wikipedia API | This Day in History | Free |
| Google News RSS | Per-ticker headlines | Free |
| yfinance | Per-ticker news | Free |
| Gmail IMAP | Full newsletter and scanner email bodies | Free |
| Anthropic Claude API | AI summarization (batch — one call for all positions) | Pay per use |

## Architecture

```
news_digest/
├── news_digest.py       # Main script
├── config.py            # Credentials (never committed)
├── config_example.py    # Template
└── .gitignore
```

Reads open equity positions from `../ib_execution/positions.db` and crypto positions live from Coinbase Advanced Trade API.

## Setup

```bash
pip3 install feedparser anthropic coinbase-advanced-py yfinance requests
cp config_example.py config.py
# Fill in config.py with your credentials
python3 news_digest.py --mode daily    # test daily
python3 news_digest.py --mode weekly   # test weekly
```

## Cron Installation

```bash
# Daily at 5:30 AM CT (11:30 UTC)
30 5 * * 1-5 cd /path/to/news_digest && python3 news_digest.py --mode daily >> news_digest.log 2>&1

# Weekly close at 3:30 PM CT (21:30 UTC) on Fridays
30 15 * * 5 cd /path/to/news_digest && python3 news_digest.py --mode weekly >> news_digest.log 2>&1
```

## Phase 2 Roadmap

- Benzinga Pro API for real-time financial news ($99–199/mo)
- SendGrid subscriber distribution
- Branded Grist Mill Capital public newsletter

## Disclaimer

For informational purposes only. Not investment advice. Grist Mill Capital.

## License

MIT
