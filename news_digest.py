#!/usr/bin/env python3
"""
GMC News Digest — news_digest.py
Daily:  5:30 AM CT Mon-Fri  →  python3 news_digest.py --mode daily
Weekly: 3:30 PM CT Friday   →  python3 news_digest.py --mode weekly
"""

import argparse
import base64
import imaplib
import email
import json
import os
import re
import smtplib
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import anthropic
import feedparser
import requests
import yfinance as yf

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

# Performance tracker (Phase 3) — import from ib_execution
_IB_EXEC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ib_execution")
sys.path.insert(0, _IB_EXEC_DIR)
try:
    from performance_tracker import get_performance_summary
    PERF_AVAILABLE = True
except Exception as _perf_err:
    print(f"[WARN] performance_tracker import failed: {_perf_err}")
    PERF_AVAILABLE = False

CT = ZoneInfo("America/Chicago")
NOW_CT = datetime.now(CT)
TODAY_STR = NOW_CT.strftime("%A, %B %-d, %Y")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LOGO_PATH = os.path.expanduser(
    "~/Desktop/gristmillcapital/Logos/logo-heritage-emblem-logo-for-/"
    "GRIST MILL CAPITAL_HORIZONTAL LOGO.png"
)

def load_logo_b64():
    try:
        with open(LOGO_PATH, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:
        return None

# ---------------------------------------------------------------------------
# 1. EQUITY POSITIONS
# ---------------------------------------------------------------------------
def get_equity_positions():
    db_path = os.path.join(SCRIPT_DIR, config.IB_POSITIONS_DB)
    if not os.path.exists(db_path):
        print(f"[WARN] IB positions DB not found: {db_path}")
        return []
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT ticker, source, entry_date, entry_price, direction
            FROM open_positions
            WHERE status = 'OPEN'
        """)
        rows = cur.fetchall()
        conn.close()
        return [{"ticker": r[0], "source": r[1], "entry_date": r[2],
                 "entry_price": r[3], "direction": r[4]} for r in rows]
    except Exception as e:
        print(f"[WARN] Could not read equity positions: {e}")
        return []

# ---------------------------------------------------------------------------
# 2. CRYPTO POSITIONS
# ---------------------------------------------------------------------------
def get_crypto_positions():
    try:
        from coinbase.rest import RESTClient
        key_path = os.path.join(SCRIPT_DIR, config.COINBASE_CDP_KEY)
        with open(key_path) as f:
            creds = json.load(f)
        client = RESTClient(api_key=creds["name"], api_secret=creds["privateKey"])
        accounts = client.get_accounts()
        positions = []
        for acct in accounts.accounts:
            ab = acct.available_balance
            if isinstance(ab, dict):
                balance = float(ab.get("value", 0))
                currency = ab.get("currency", "")
            else:
                balance = float(getattr(ab, "value", 0))
                currency = getattr(ab, "currency", "")
            if balance > 0.0001 and currency not in ("USD", "USDC"):
                positions.append({"ticker": currency, "balance": balance, "direction": "LONG"})
        return positions
    except Exception as e:
        print(f"[WARN] Could not read crypto positions: {e}")
        return []

# ---------------------------------------------------------------------------
# 3. NEWS — Google News RSS
# ---------------------------------------------------------------------------
def fetch_rss_news(ticker, hours=24):
    url = f"https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    headlines = []
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries[:10]:
            published = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            if published and published < cutoff:
                continue
            headlines.append(entry.title)
    except Exception as e:
        print(f"[WARN] RSS failed for {ticker}: {e}")
    return headlines

# ---------------------------------------------------------------------------
# 4. NEWS — yfinance
# ---------------------------------------------------------------------------
def fetch_yf_news(ticker):
    headlines = []
    try:
        t = yf.Ticker(ticker)
        news = t.news or []
        cutoff = time.time() - 86400
        for item in news[:10]:
            if item.get("providerPublishTime", 0) < cutoff:
                continue
            content = item.get("content", {})
            title = content.get("title") or item.get("title", "")
            if title:
                headlines.append(title)
    except Exception as e:
        print(f"[WARN] yfinance failed for {ticker}: {e}")
    return headlines

# ---------------------------------------------------------------------------
# 5. NEWS — Gmail IMAP
# ---------------------------------------------------------------------------
def fetch_gmail_news(hours=24):
    headlines = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
        mail.login(config.IMAP_USER, config.IMAP_PASSWORD)
        mail.select("inbox")
        since_date = (datetime.now() - timedelta(hours=hours)).strftime("%d-%b-%Y")
        _, data = mail.search(None, f'(SINCE "{since_date}")')
        ids = data[0].split()[-50:]
        keywords = ["market", "stock", "fund", "fed", "earnings", "crypto",
                    "8-K", "insider", "scanner", "signal", "alert", "economic",
                    "GDP", "CPI", "inflation", "rate", "trade"]
        for uid in ids:
            try:
                _, msg_data = mail.fetch(uid, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                from email.header import decode_header as _dh
                raw_subj = msg.get("Subject", "")
                raw_sender = msg.get("From", "")
                # Decode encoded subjects (handles UTF-8 emoji etc)
                parts = _dh(raw_subj)
                subject = ""
                for part, enc in parts:
                    if isinstance(part, bytes):
                        subject += part.decode(enc or "utf-8", errors="replace")
                    else:
                        subject += part
                sender = raw_sender.split("<")[0].strip().strip('"')
                if any(kw.lower() in subject.lower() for kw in keywords):
                    headlines.append(f"[{sender}] {subject}")
            except Exception:
                continue
        mail.logout()
    except Exception as e:
        print(f"[WARN] Gmail IMAP failed: {e}")
    return headlines

# ---------------------------------------------------------------------------
# 6. MACRO CALENDAR — ForexFactory (today)
# ---------------------------------------------------------------------------
def fetch_macro_calendar():
    """Fetch today's economic calendar from FMP (primary) with ForexFactory fallback."""
    today_date = NOW_CT.date()
    events = []
    # --- PRIMARY: FMP ---
    try:
        from_str = today_date.strftime('%Y-%m-%d')
        to_str   = today_date.strftime('%Y-%m-%d')
        url = (f'https://financialmodelingprep.com/stable/economic-calendar'
               f'?from={from_str}&to={to_str}&apikey={config.FMP_API_KEY}')
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for ev in data:
                    country = ev.get('country', '')
                    if country and country.upper() not in ('US', ''):
                        continue
                    raw_time = ev.get('time', '') or ''
                    try:
                        dt = datetime.strptime(raw_time, '%Y-%m-%d %H:%M:%S')
                        dt_ct = dt.replace(tzinfo=timezone.utc).astimezone(CT)
                        time_label = dt_ct.strftime('%-I:%M %p CT')
                    except Exception:
                        time_label = raw_time or 'All Day'
                    events.append({
                        'time':     time_label,
                        'event':    ev.get('event', ''),
                        'estimate': str(ev.get('estimate') or '--'),
                        'previous': str(ev.get('previous') or '--'),
                        'actual':   str(ev.get('actual')   or '--'),
                        'impact':   str(ev.get('impact')   or '')
                    })
                events.sort(key=lambda x: x['time'])
                print(f'  [FMP] {len(events)} macro events today')
                return events
        print(f'[WARN] FMP calendar returned {resp.status_code} -- falling back to ForexFactory')
    except Exception as e:
        print(f'[WARN] FMP calendar failed: {e} -- falling back to ForexFactory')
    # --- FALLBACK: ForexFactory ---
    try:
        url = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json'
        resp = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        if resp.status_code != 200:
            print(f'[WARN] ForexFactory returned {resp.status_code} -- macro calendar unavailable, check investing.com')
            return None
        for ev in resp.json():
            if ev.get('currency') != 'USD':
                continue
            try:
                ev_date = datetime.strptime(ev.get('date', ''), '%m-%d-%Y').date()
            except Exception:
                continue
            if ev_date != today_date:
                continue
            raw_time = ev.get('time', '').upper()
            time_label = raw_time.replace('AM', ' AM CT').replace('PM', ' PM CT') if raw_time else 'All Day'
            events.append({
                'time': time_label, 'event': ev.get('title', ''),
                'estimate': ev.get('forecast') or '--', 'previous': ev.get('previous') or '--',
                'actual': ev.get('actual') or '--', 'impact': ev.get('impact', '')
            })
        events.sort(key=lambda x: x['time'])
    except Exception as e:
        print(f'[WARN] ForexFactory calendar failed: {e}')
    return events


def fetch_yesterdays_actuals():
    """Fetch yesterday's released economic data from FMP with ForexFactory fallback."""
    yesterday_date = (NOW_CT - timedelta(days=1)).date()
    results = []
    # --- PRIMARY: FMP ---
    try:
        from_str = yesterday_date.strftime('%Y-%m-%d')
        url = (f'https://financialmodelingprep.com/stable/economic-calendar'
               f'?from={from_str}&to={from_str}&apikey={config.FMP_API_KEY}')
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for ev in data:
                    country = ev.get('country', '')
                    if country and country.upper() not in ('US', ''):
                        continue
                    actual = ev.get('actual')
                    if actual is None or str(actual).strip() in ('', '--', 'None'):
                        continue
                    results.append({
                        'event':    ev.get('event', ''),
                        'estimate': str(ev.get('estimate') or '--'),
                        'previous': str(ev.get('previous') or '--'),
                        'actual':   str(actual)
                    })
                print(f'  [FMP] {len(results)} actuals from yesterday')
                return results
    except Exception as e:
        print(f'[WARN] FMP yesterdays actuals failed: {e}')
    # --- FALLBACK: ForexFactory ---
    try:
        url = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json'
        resp = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        if resp.status_code != 200:
            return []
        for ev in resp.json():
            if ev.get('currency') != 'USD':
                continue
            actual = ev.get('actual') or ''
            if not actual or actual == '--':
                continue
            try:
                ev_date = datetime.strptime(ev.get('date', ''), '%m-%d-%Y').date()
            except Exception:
                continue
            if ev_date != yesterday_date:
                continue
            results.append({
                'event': ev.get('title', ''), 'estimate': ev.get('forecast') or '--',
                'previous': ev.get('previous') or '--', 'actual': actual
            })
    except Exception as e:
        print(f'[WARN] Yesterday actuals fallback failed: {e}')
    return results

def fetch_this_day_in_history():
    try:
        month = NOW_CT.strftime("%-m")
        day = NOW_CT.strftime("%-d")
        url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{month}/{day}"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "GMCNewsDigest/1.0"})
        if resp.status_code != 200:
            return []
        events = resp.json().get("events", [])
        candidates = [f"{e['year']}: {e['text']}" for e in events[:40]]
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        prompt = f"""Today is {TODAY_STR}. Pick the 3 most interesting historical events from this list for a financial newsletter audience.
Prefer events related to markets, economics, business, technology, or geopolitics.
Return ONLY a JSON array of 3 strings formatted as "YEAR: description." No preamble, no markdown.

Events:
{chr(10).join(candidates)}"""
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            timeout=30,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip().strip("```json").strip("```").strip()
        return json.loads(text)
    except Exception as e:
        print(f"[WARN] This day in history failed: {e}")
        return []


def fetch_top_news():
    """Fetch top 3 must-know world news items via Google News RSS + Claude."""
    headlines = []
    try:
        url = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
        feed = feedparser.parse(url)
        for entry in feed.entries[:25]:
            headlines.append(entry.title)
    except Exception as e:
        print(f"[WARN] Top news RSS failed: {e}")
        return []
    if not headlines:
        return []
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        prompt = (
            f"Today is {TODAY_STR}. From this list of current news headlines, "
            "pick the 3 most important things happening in the world today that "
            "people genuinely need to know. Include any category: geopolitics, "
            "science, disasters, major policy, technology, business, or other "
            "significant events. Do NOT limit to financial or market news. "
            "Return ONLY a JSON array of 3 strings. Each string must be a clean, "
            "factual one-sentence summary (not just the raw headline). "
            "No preamble, no markdown backticks.\n\nHeadlines:\n"
            + chr(10).join(headlines)
        )
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            timeout=30,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip().strip("```json").strip("```").strip()
        return json.loads(text)
    except Exception as e:
        print(f"[WARN] Top news summarization failed: {e}")
        return []


# ---------------------------------------------------------------------------
# 7. BATCH CLAUDE SUMMARIZATION — one API call for all positions
# ---------------------------------------------------------------------------
def summarize_all_positions(positions_data, weekly=False):
    if not positions_data:
        return {}
    horizon = "this week" if weekly else "the last 24 hours"
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    blocks = []
    for p in positions_data:
        hl = chr(10).join(f"  - {h}" for h in p["headlines"][:12]) or "  (no headlines)"
        company = p.get('company_name', p['ticker'])
        blocks.append(f"TICKER: {p['ticker']} | COMPANY: {company} | {p['direction']} | {p['source']}\n{hl}")
    prompt = f"""You are a financial research assistant for Grist Mill Capital.
For each ticker below write a 2-3 sentence facts-only summary covering {horizon}.
Rules: facts only, no opinions, no predictions. If no material news write exactly: "No material news {horizon}."
Return ONLY a JSON object where keys are ticker symbols and values are summary strings. No markdown, no preamble.

{chr(10).join(blocks)}"""
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            timeout=120,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip().strip("```json").strip("```").strip()
        return json.loads(text)
    except Exception as e:
        print(f"[WARN] Batch summarization error: {e}")
        return {p["ticker"]: "Summary unavailable." for p in positions_data}

def summarize_weekly_macro(macro_events, gmail_headlines):
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    events_text = "\n".join(
        f"- {e['event']}: actual={e.get('actual','?')} vs estimate={e.get('estimate','?')}"
        for e in macro_events if e.get("actual") not in (None, "—", "")
    ) or "No macro releases with actuals."
    emails_text = "\n".join(f"- {h}" for h in gmail_headlines[:20]) or "No emails."
    prompt = f"""You are a financial research assistant for Grist Mill Capital.
Week ending {TODAY_STR}.

Macro releases this week:
{events_text}

Financial email subjects this week:
{emails_text}

Write a concise weekly macro recap in 4-6 sentences. Facts only, no opinions. Highlight market-moving releases and Fed signals."""
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            timeout=60,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"[Weekly macro summary unavailable: {e}]"

# ---------------------------------------------------------------------------
# 8. HTML HELPERS
# ---------------------------------------------------------------------------
def gmc_logo_html(height=90):
    b64 = load_logo_b64()
    if b64:
        return f"<img src='data:image/png;base64,{b64}' style='height:{height}px;display:block;' alt='Grist Mill Capital'>"
    return "<span style='font-size:22px;font-weight:bold;color:#A52818;letter-spacing:1px;'>GRIST MILL CAPITAL</span>"

def gmc_header(subtitle):
    logo = gmc_logo_html()
    return f"""<div style='font-family:Arial,sans-serif;max-width:700px;margin:0 auto;'>
<div style='background:white;padding:28px 30px 18px;border-bottom:3px solid #A52818;'>
  {logo}
  <p style='color:#555;margin:10px 0 0;font-size:13px;letter-spacing:0.3px;'>{subtitle} &nbsp;&middot;&nbsp; {TODAY_STR}</p>
</div>"""

def gmc_footer(label):
    return f"""<div style='padding:16px 30px;background:#1C3560;margin-top:10px;'>
  <p style='color:#aab4cc;font-size:11px;margin:0;'>Grist Mill Capital &middot; gristmillcapital.co &middot; {label} {NOW_CT.strftime("%-I:%M %p CT")}</p>
</div>
</div>"""

def macro_table(events):
    if events is None:
        return "<p style='color:#c0392b;font-weight:bold;'>&#9888; Macro calendar unavailable (data source rate limited). Check <a href='https://www.investing.com/economic-calendar/' style='color:#c0392b;'>investing.com/economic-calendar</a> manually.</p>"
    if not events:
        return "<p style='color:#666;font-style:italic;'>No US macro releases scheduled today.</p>"
    rows = ["<table style='border-collapse:collapse;width:100%;font-size:14px;'>",
            "<tr style='background:#1C3560;color:white;'>"]
    for col, align in [("Time CT","left"),("Event","left"),("Estimate","right"),("Previous","right"),("Actual","right")]:
        rows.append(f"<th style='padding:7px 10px;text-align:{align};'>{col}</th>")
    rows.append("</tr>")
    for i, ev in enumerate(events):
        bg = "#f9f9f9" if i % 2 == 0 else "#ffffff"
        rows += [
            f"<tr style='background:{bg};'>",
            f"<td style='padding:6px 10px;'>{ev['time']}</td>",
            f"<td style='padding:6px 10px;'>&#127482;&#127480; {ev['event']}</td>",
            f"<td style='padding:6px 10px;text-align:right;'>{ev['estimate']}</td>",
            f"<td style='padding:6px 10px;text-align:right;'>{ev['previous']}</td>",
            f"<td style='padding:6px 10px;text-align:right;font-weight:bold;'>{ev['actual']}</td>",
            "</tr>"
        ]
    rows.append("</table>")
    return "\n".join(rows)

def yesterdays_table(actuals):
    if not actuals:
        return "<p style='color:#666;font-style:italic;'>No US macro releases yesterday.</p>"
    rows = ["<table style='border-collapse:collapse;width:100%;font-size:14px;'>",
            "<tr style='background:#1C3560;color:white;'>"]
    for col, align in [("Event","left"),("Estimate","right"),("Previous","right"),("Actual","right")]:
        rows.append(f"<th style='padding:7px 10px;text-align:{align};'>{col}</th>")
    rows.append("</tr>")
    for i, ev in enumerate(actuals):
        bg = "#f9f9f9" if i % 2 == 0 else "#ffffff"
        rows += [
            f"<tr style='background:{bg};'>",
            f"<td style='padding:6px 10px;'>&#127482;&#127480; {ev['event']}</td>",
            f"<td style='padding:6px 10px;text-align:right;'>{ev['estimate']}</td>",
            f"<td style='padding:6px 10px;text-align:right;'>{ev['previous']}</td>",
            f"<td style='padding:6px 10px;text-align:right;font-weight:bold;'>{ev['actual']}</td>",
            "</tr>"
        ]
    rows.append("</table>")
    return "\n".join(rows)

def section_div(title, content, border=True):
    border_style = "border-bottom:2px solid #A52818;" if border else "border-bottom:1px solid #eee;"
    return f"""<div style='padding:28px 30px;{border_style}'>
  <h2 style='color:#1C3560;font-size:15px;margin:0 0 14px;text-transform:uppercase;letter-spacing:1px;'>{title}</h2>
  {content}
</div>"""

def position_card(ticker, direction, source, entry_date, summary):
    color = "#c0392b" if direction in ("SHORT", "BEAR") else "#1a7a3c"
    meta = source + (f" &middot; Entry {entry_date}" if entry_date else "")
    return f"""<div style='margin-bottom:16px;padding:14px 16px;border-left:4px solid {color};background:#fafafa;border-radius:2px;'>
  <div style='display:flex;justify-content:space-between;align-items:center;'>
    <span style='font-size:17px;font-weight:bold;color:#1C3560;'>{ticker}</span>
    <span style='font-size:11px;color:#888;'>{meta}</span>
  </div>
  <span style='font-size:11px;font-weight:bold;color:{color};text-transform:uppercase;'>{direction}</span>
  <p style='margin:8px 0 0;font-size:14px;color:#333;line-height:1.55;'>{summary}</p>
</div>"""

def build_position_cards(equity, crypto, summaries):
    cards = []
    for p in equity:
        ticker = p["ticker"]
        summary = summaries.get(ticker, "Summary unavailable.")
        cards.append(position_card(ticker, p.get("direction","?"), p.get("source",""), p.get("entry_date",""), summary))
    for p in crypto:
        ticker = p["ticker"]
        summary = summaries.get(ticker, "Summary unavailable.")
        cards.append(position_card(ticker, "LONG", "CRYPTO", "", summary))
    return "\n".join(cards) if cards else "<p style='color:#666;font-style:italic;'>No open positions.</p>"

def history_html(history_items):
    items = "".join(f"<li style='margin-bottom:8px;'>{item}</li>" for item in history_items)
    return f"<ul style='margin:0;padding-left:20px;font-size:14px;color:#444;line-height:1.6;'>{items}</ul>"

def inbox_html(gmail):
    items = "".join(f"<li style='margin-bottom:5px;font-size:13px;'>{h}</li>" for h in gmail[:10])
    return f"<ul style='margin:0;padding-left:18px;color:#444;'>{items}</ul>"


def need_to_know_html(items):
    if not items:
        return "<p style='color:#666;font-style:italic;'>No top news available.</p>"
    rows = ""
    for i, item in enumerate(items, 1):
        rows += (
            f"<div style='display:flex;gap:12px;margin-bottom:12px;align-items:flex-start;'>"
            f"<span style='font-size:18px;font-weight:bold;color:#A52818;min-width:24px;'>{i}.</span>"
            f"<p style='margin:0;font-size:14px;color:#333;line-height:1.55;'>{item}</p>"
            "</div>"
        )
    return rows


# ---------------------------------------------------------------------------
# 9a. GMC PERFORMANCE RENDERERS (Phase 3)
# ---------------------------------------------------------------------------
def _pct_color(val):
    """Return CSS color for a percentage: green positive, red negative, gray None."""
    if val is None:
        return "#999"
    return "#1a7a3c" if val >= 0 else "#c0392b"

def _fmt_pct(val, show_sign=True):
    """Format a percentage value with color. Returns HTML span."""
    if val is None:
        return "<span style='color:#999;'>--</span>"
    sign = "+" if val >= 0 and show_sign else ""
    color = _pct_color(val)
    return f"<span style='color:{color};font-weight:bold;'>{sign}{val:.2f}%</span>"

def _fmt_dollar(val):
    """Format a dollar value. Returns string."""
    if val is None:
        return "--"
    return f"${val:,.2f}"

def _cap_color(count, max_pos=20):
    """Return color for open position count."""
    if count >= 18:
        return "#c0392b"   # red
    if count >= 15:
        return "#e67e22"   # orange
    return "#1a7a3c"       # green

def render_portfolio_snapshot(perf):
    """Render the portfolio snapshot subsection HTML."""
    if perf is None:
        return "<p style='color:#999;font-style:italic;'>Performance data unavailable.</p>"

    header = perf.get("portfolio_header", {})
    bedrock = perf.get("bedrock", {})
    ea = perf.get("event_alpha", {})
    da = perf.get("digital_alpha", {})

    open_count = header.get("open_positions_count", 0)
    max_pos = header.get("max_positions", 20)
    slots_free = header.get("slots_free", 0)
    cap_color = _cap_color(open_count, max_pos)

    lines = []

    # Open positions counter
    lines.append(
        f"<div style='margin-bottom:16px;padding:10px 14px;background:#f4f4f4;border-radius:4px;'>"
        f"<span style='font-size:16px;font-weight:bold;color:{cap_color};'>"
        f"OPEN POSITIONS: {open_count} / {max_pos}</span>"
        f"<span style='font-size:13px;color:#666;margin-left:12px;'>({slots_free} slots free)</span>"
        f"</div>"
    )

    # --- Bedrock ---
    br_ret = _fmt_pct(bedrock.get("return_pct"))
    br_cost = _fmt_dollar(bedrock.get("cost_basis"))
    br_val = _fmt_dollar(bedrock.get("current_value"))
    spy_br = _fmt_pct(bedrock.get("spy_return_pct"))
    qqq_br = _fmt_pct(bedrock.get("qqq_return_pct"))
    lines.append(
        f"<div style='margin-bottom:10px;padding:8px 0;border-bottom:1px solid #eee;font-size:14px;'>"
        f"<strong style='color:#1C3560;'>BEDROCK</strong>&emsp;"
        f"{br_ret}&emsp;|&emsp;cost {br_cost}&emsp;|&emsp;value {br_val}"
        f"&emsp;|&emsp;SPY {spy_br}&ensp;QQQ {qqq_br}"
        f"</div>"
    )

    # --- Event Alpha ---
    ea_avg = ea.get("avg_return_pct")
    ea_closed = ea.get("closed_trades", 0)
    ea_wr = ea.get("win_rate_pct")
    ea_alpha = ea.get("alpha_vs_spy")
    if ea_closed > 0 and ea_avg is not None:
        ea_ret_str = _fmt_pct(ea_avg)
        wr_str = f"{ea_wr:.0f}%" if ea_wr is not None else "--"
        alpha_str = _fmt_pct(ea_alpha) if ea_alpha is not None else "<span style='color:#999;'>N/A</span>"
        ea_detail = (
            f"{ea_ret_str}&emsp;|&emsp;{ea_closed} closed&ensp;{wr_str} win"
            f"&emsp;|&emsp;SPY alpha {alpha_str}"
        )
    else:
        ea_detail = "<span style='color:#999;'>No closed trades yet</span>"
    lines.append(
        f"<div style='margin-bottom:10px;padding:8px 0;border-bottom:1px solid #eee;font-size:14px;'>"
        f"<strong style='color:#1C3560;'>EVENT ALPHA</strong>&emsp;{ea_detail}"
        f"</div>"
    )

    # --- Digital Alpha ---
    da_ret = da.get("return_pct")
    da_val = da.get("current_value")
    da_btc = da.get("btc_hold_return_pct")
    da_usdc = da.get("usdc_staking_return_pct")
    if da_val is not None:
        da_ret_str = _fmt_pct(da_ret)
        da_val_str = _fmt_dollar(da_val)
        btc_str = _fmt_pct(da_btc) if da_btc is not None else "<span style='color:#999;'>--</span>"
        usdc_str = _fmt_pct(da_usdc) if da_usdc is not None else "<span style='color:#999;'>--</span>"
        da_detail = (
            f"{da_ret_str}&emsp;|&emsp;value {da_val_str}"
            f"&emsp;|&emsp;BTC {btc_str}&ensp;USDC staking {usdc_str}"
        )
    else:
        btc_str = _fmt_pct(da_btc) if da_btc is not None else "<span style='color:#999;'>--</span>"
        usdc_str = _fmt_pct(da_usdc) if da_usdc is not None else "<span style='color:#999;'>--</span>"
        da_detail = (
            f"<span style='color:#999;'>Awaiting live trades</span>"
            f"&emsp;|&emsp;BTC {btc_str}&ensp;USDC staking {usdc_str}"
        )
    lines.append(
        f"<div style='padding:8px 0;font-size:14px;'>"
        f"<strong style='color:#1C3560;'>DIGITAL ALPHA</strong>&emsp;{da_detail}"
        f"</div>"
    )

    return "\n".join(lines)


def render_open_positions_table(perf):
    """Render compact open positions table sorted by days_remaining ascending."""
    if perf is None:
        return ""

    positions = perf.get("open_positions", [])
    if not positions:
        return "<p style='color:#666;font-style:italic;margin-top:14px;'>No open positions.</p>"

    # Sort by days_remaining ascending (None last)
    def _sort_key(p):
        dr = p.get("days_remaining")
        return dr if dr is not None else 9999
    positions = sorted(positions, key=_sort_key)

    rows = [
        "<table style='border-collapse:collapse;width:100%;font-size:13px;margin-top:14px;'>",
        "<tr style='background:#1C3560;color:white;'>",
    ]
    for col, align in [("Ticker","left"),("Signal","left"),("Dir","center"),
                        ("Day","center"),("P&L%","right"),("vs Expected","right")]:
        rows.append(f"<th style='padding:6px 8px;text-align:{align};'>{col}</th>")
    rows.append("</tr>")

    for i, pos in enumerate(positions):
        bg = "#f9f9f9" if i % 2 == 0 else "#ffffff"
        ticker = pos.get("ticker", "?")
        signal = pos.get("source", "?")
        direction = pos.get("direction", "?")
        dir_color = "#c0392b" if direction in ("SHORT",) else "#1a7a3c"
        dir_label = "S" if direction == "SHORT" else "L"

        days_held = pos.get("days_held")
        exp_hold = pos.get("expected_hold_days")
        if days_held is not None and exp_hold is not None:
            day_str = f"{days_held}/{exp_hold}"
        elif days_held is not None:
            day_str = str(days_held)
        else:
            day_str = "--"

        ret = pos.get("return_pct")
        if ret is not None:
            ret_color = "#1a7a3c" if ret >= 0 else "#c0392b"
            sign = "+" if ret >= 0 else ""
            ret_str = f"<span style='color:{ret_color};font-weight:bold;'>{sign}{ret:.1f}%</span>"
        else:
            ret_str = "<span style='color:#999;'>--</span>"

        vs = pos.get("vs_prorata")
        if vs is not None:
            vs_color = "#1a7a3c" if vs >= 0 else "#c0392b"
            vs_sign = "+" if vs >= 0 else ""
            vs_str = f"<span style='color:{vs_color};'>{vs_sign}{vs:.1f}%</span>"
        else:
            vs_str = "<span style='color:#999;'>--</span>"

        rows += [
            f"<tr style='background:{bg};'>",
            f"<td style='padding:5px 8px;font-weight:bold;color:#1C3560;'>{ticker}</td>",
            f"<td style='padding:5px 8px;font-size:11px;color:#666;'>{signal}</td>",
            f"<td style='padding:5px 8px;text-align:center;'><span style='color:{dir_color};font-weight:bold;font-size:11px;'>{dir_label}</span></td>",
            f"<td style='padding:5px 8px;text-align:center;'>{day_str}</td>",
            f"<td style='padding:5px 8px;text-align:right;'>{ret_str}</td>",
            f"<td style='padding:5px 8px;text-align:right;'>{vs_str}</td>",
            "</tr>",
        ]

    rows.append("</table>")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# 9. BUILD EMAILS
# ---------------------------------------------------------------------------
def build_daily_email(equity, crypto, macro, gmail, yesterdays, history, top_news=None, perf=None):
    # Fetch all headlines first (no API calls)
    all_pos = [(p["ticker"], p.get("direction","?"), p.get("source",""), p.get("entry_date",""), "equity") for p in equity]
    all_pos += [(p["ticker"], "LONG", "CRYPTO", "", "crypto") for p in crypto]

    batch_input = []
    pos_headlines = {}
    for ticker, direction, source, entry_date, asset_type in all_pos:
        rss = fetch_rss_news(ticker)
        yf_ticker = ticker if asset_type == "equity" else f"{ticker}-USD"
        yfn = fetch_yf_news(yf_ticker)
        headlines = list(dict.fromkeys(rss + yfn))
        pos_headlines[ticker] = headlines
        # Fetch verified company name from yfinance to prevent Claude hallucinating wrong company
        try:
            import yfinance as yf
            info = yf.Ticker(yf_ticker).info
            company_name = info.get("longName") or info.get("shortName") or ticker
        except Exception:
            company_name = ticker
        batch_input.append({"ticker": ticker, "company_name": company_name, "direction": direction, "source": source, "headlines": headlines})

    summaries = summarize_all_positions(batch_input, weekly=False)

    # Section numbering
    sec_num = 1
    sections = [gmc_header("Morning Intelligence Brief")]

    # GMC Performance (Phase 3) — first section after header
    if perf is not None:
        perf_html = render_portfolio_snapshot(perf) + render_open_positions_table(perf)
        sections.append(section_div("GMC Performance", perf_html))
        sec_num += 1

    # Macro today
    num_symbols = ["&#x2460;","&#x2461;","&#x2462;","&#x2463;","&#x2464;","&#x2465;","&#x2466;"]
    sections.append(section_div(f"{num_symbols[sec_num - 1]} Macro Calendar &mdash; Today", macro_table(macro)))
    sec_num += 1

    # Yesterday's actuals
    if yesterdays:
        sections.append(section_div(f"{num_symbols[sec_num - 1]} Yesterday&rsquo;s Numbers", yesterdays_table(yesterdays)))
        sec_num += 1

    # This day in history
    if history:
        sections.append(f"""<div style='padding:16px 30px;background:#fdf8f0;border-bottom:1px solid #e8e0d0;'>
  <h3 style='color:#1C3560;font-size:13px;margin:0 0 10px;text-transform:uppercase;letter-spacing:1px;'>&#128197; This Day in History</h3>
  {history_html(history)}
</div>""")
        sec_num += 1

    # Need to Know
    sections.append(section_div(f"{num_symbols[sec_num - 1]} Need to Know &mdash; Today", need_to_know_html(top_news)))
    sec_num += 1

    # Position intelligence — include open count in title
    open_count = perf.get("portfolio_header", {}).get("open_positions_count", len(equity)) if perf else len(equity)
    cards = build_position_cards(equity, crypto, summaries)
    sections.append(section_div(
        f"{num_symbols[sec_num - 1]} Position Intelligence &mdash; {open_count} / 20 open",
        cards
    ))

    # Inbox highlights
    if gmail:
        sections.append(f"""<div style='padding:16px 30px;background:#f0f4ff;border-top:1px solid #dce4f5;'>
  <h3 style='color:#1C3560;font-size:13px;margin:0 0 8px;text-transform:uppercase;letter-spacing:1px;'>Inbox Highlights</h3>
  {inbox_html(gmail)}
</div>""")

    sections.append(gmc_footer("Generated"))
    return "\n".join(sections)

def build_weekly_email(equity, crypto, macro, gmail, yesterdays, history, top_news=None):
    # Collect all headlines for batch
    all_pos = [(p["ticker"], p.get("direction","?"), p.get("source",""), p.get("entry_date",""), "equity") for p in equity]
    all_pos += [(p["ticker"], "LONG", "CRYPTO", "", "crypto") for p in crypto]

    batch_input = []
    for ticker, direction, source, entry_date, asset_type in all_pos:
        rss = fetch_rss_news(ticker, hours=120)
        yf_ticker = ticker if asset_type == "equity" else f"{ticker}-USD"
        yfn = fetch_yf_news(yf_ticker)
        headlines = list(dict.fromkeys(rss + yfn))
        batch_input.append({"ticker": ticker, "direction": direction, "source": source, "headlines": headlines})

    summaries = summarize_all_positions(batch_input, weekly=True)
    macro_summary = summarize_weekly_macro(macro, gmail)

    # Check for Carr Ward photo
    carr_ward = os.path.expanduser("~/Desktop/gristmillcapital/carr_ward_mill.jpg")
    if os.path.exists(carr_ward):
        with open(carr_ward, "rb") as f:
            cw_b64 = base64.b64encode(f.read()).decode()
        header_img = f"<img src='data:image/jpeg;base64,{cw_b64}' style='width:100%;max-height:220px;object-fit:cover;display:block;' alt='Grist Mill'>"
    else:
        header_img = gmc_logo_html(height=90)

    sections = [f"""<div style='font-family:Arial,sans-serif;max-width:700px;margin:0 auto;'>
<div style='background:white;padding:28px 30px 18px;border-bottom:3px solid #A52818;'>
  {header_img}
  <p style='color:#555;margin:10px 0 0;font-size:13px;'>Weekly Close Brief &nbsp;&middot;&nbsp; {TODAY_STR}</p>
</div>"""]

    sections.append(section_div("&#x2460; Week in Review &mdash; Macro",
                                f"<p style='font-size:14px;color:#333;line-height:1.6;margin:0;'>{macro_summary}</p>"))
    sections.append(section_div("&#x2461; This Week&rsquo;s Macro Releases", macro_table(macro)))

    if history:
        sections.append(f"""<div style='padding:16px 30px;background:#fdf8f0;border-bottom:1px solid #e8e0d0;'>
  <h3 style='color:#1C3560;font-size:13px;margin:0 0 10px;text-transform:uppercase;letter-spacing:1px;'>&#128197; This Day in History</h3>
  {history_html(history)}
</div>""")

    if top_news:
        sections.append(section_div("&#x2462; Need to Know &mdash; This Week", need_to_know_html(top_news)))
    cards = build_position_cards(equity, crypto, summaries)
    sections.append(section_div("&#x2463; Open Positions &mdash; Weekly Digest", cards))

    if gmail:
        sections.append(f"""<div style='padding:16px 30px;background:#f0f4ff;border-top:1px solid #dce4f5;'>
  <h3 style='color:#1C3560;font-size:13px;margin:0 0 8px;text-transform:uppercase;letter-spacing:1px;'>Inbox Highlights</h3>
  {inbox_html(gmail)}
</div>""")

    sections.append(gmc_footer("Weekly Close"))
    return "\n".join(sections)

# ---------------------------------------------------------------------------
# 10. SEND EMAIL
# ---------------------------------------------------------------------------
def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_SENDER
    msg["To"] = config.EMAIL_RECIPIENT
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
        server.sendmail(config.EMAIL_SENDER, config.EMAIL_RECIPIENT, msg.as_string())
    print(f"[OK] Email sent to {config.EMAIL_RECIPIENT}")

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["daily", "weekly"], default="daily")
    args = parser.parse_args()

    print(f"[{NOW_CT.strftime('%H:%M CT')}] GMC News Digest — mode: {args.mode}")

    print("  Fetching equity positions...")
    equity = get_equity_positions()
    print(f"  → {len(equity)} open equity positions")

    print("  Fetching crypto positions...")
    crypto = get_crypto_positions()
    print(f"  → {len(crypto)} open crypto positions")

    print("  Fetching macro calendar...")
    macro = fetch_macro_calendar()
    print(f"  → {len(macro)} macro events today")

    print("  Fetching yesterday's actuals...")
    yesterdays = fetch_yesterdays_actuals()
    print(f"  → {len(yesterdays)} actuals from yesterday")

    print("  Fetching top news...")
    top_news = fetch_top_news()
    print(f"  -> {len(top_news)} need-to-know items")
    print("  Fetching this day in history...")
    history = fetch_this_day_in_history()
    print(f"  → {len(history)} history items")

    print("  Fetching Gmail highlights...")
    gmail = fetch_gmail_news(hours=24 if args.mode == "daily" else 120)
    print(f"  → {len(gmail)} relevant emails")

    # Performance tracker (Phase 3) — daily only, with timeout protection
    perf = None
    if args.mode == "daily" and PERF_AVAILABLE:
        print("  Fetching performance summary...")
        try:
            import threading
            _perf_result = [None]
            def _run_perf():
                try:
                    _perf_result[0] = get_performance_summary()
                except Exception as e:
                    print(f"[WARN] Performance tracker failed: {e}")
            _perf_thread = threading.Thread(target=_run_perf, daemon=True)
            _perf_thread.start()
            _perf_thread.join(timeout=45)
            if _perf_thread.is_alive():
                print("[WARN] Performance tracker timed out (45s) — skipping perf section")
            else:
                perf = _perf_result[0]
                print(f"  → Performance summary {'loaded' if perf else 'unavailable'}")
        except Exception as e:
            print(f"[WARN] Performance tracker error: {e}")

    print("  Summarizing positions (1 API call)...")
    if args.mode == "daily":
        subject = f"GMC Morning Brief — {TODAY_STR}"
        body = build_daily_email(equity, crypto, macro, gmail, yesterdays, history, top_news, perf=perf)
    else:
        subject = f"GMC Weekly Close — {TODAY_STR}"
        body = build_weekly_email(equity, crypto, macro, gmail, yesterdays, history, top_news)

    print("  Sending email...")
    send_email(subject, body)
    print("[DONE]")

if __name__ == "__main__":
    main()
