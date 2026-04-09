"""
GMC News Digest - Configuration Example
Copy this file to config.py and fill in your values.
Never commit config.py to git.
"""

# ---------------------------------------------------------------------------
# Email settings (Gmail app password)
# ---------------------------------------------------------------------------
EMAIL_SENDER     = "your_gmail@gmail.com"
EMAIL_PASSWORD   = "xxxx xxxx xxxx xxxx"   # Gmail App Password (not your login password)
EMAIL_RECIPIENT  = "your_delivery_address@yourdomain.com"
SMTP_SERVER      = "smtp.gmail.com"
SMTP_PORT        = 587

# ---------------------------------------------------------------------------
# Anthropic API (Claude summarization layer)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = "sk-ant-..."

# ---------------------------------------------------------------------------
# FMP API (Financial Modeling Prep) — used for economic calendar fallback
# ---------------------------------------------------------------------------
FMP_API_KEY = "your_fmp_key_here"

# ---------------------------------------------------------------------------
# Paths to position databases (relative to this script)
# ---------------------------------------------------------------------------
IB_POSITIONS_DB  = "../ib_execution/positions.db"
COINBASE_CDP_KEY = "../crypto_backtest/cdp_api_key.json"
