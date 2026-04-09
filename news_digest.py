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
        mail.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
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
    events = []
    today_date = NOW_CT.date()
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            print(f"[WARN] ForexFactory returned {resp.status_code}")
            return []
        for ev in resp.json():
            if ev.get("currency") != "USD":
                continue
            try:
                ev_date = datetime.strptime(ev.get("date", ""), "%m-%d-%Y").date()
            except Exception:
                continue
            if ev_date != today_date:
                continue
            raw_time = ev.get("time", "").upper()
            time_label = raw_time.replace("AM", " AM CT").replace("PM", " PM CT") if raw_time else "All Day"
            events.append({
                "time": time_label,
                "event": ev.get("title", ""),
                "estimate": ev.get("forecast") or "—",
                "previous": ev.get("previous") or "—",
                "actual": ev.get("actual") or "—",
                "impact": ev.get("impact", "")
            })
        events.sort(key=lambda x: x["time"])
    except Exception as e:
        print(f"[WARN] ForexFactory calendar failed: {e}")
    return events

# ---------------------------------------------------------------------------
# 6b. YESTERDAY'S ACTUALS — ForexFactory (yesterday, USD only, has actual)
# ---------------------------------------------------------------------------
def fetch_yesterdays_actuals():
    results = []
    yesterday_date = (NOW_CT - timedelta(days=1)).date()
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return []
        for ev in resp.json():
            if ev.get("currency") != "USD":
                continue
            actual = ev.get("actual") or ""
            if not actual or actual == "—":
                continue
            try:
                ev_date = datetime.strptime(ev.get("date", ""), "%m-%d-%Y").date()
            except Exception:
                continue
            if ev_date != yesterday_date:
                continue
            results.append({
                "event": ev.get("title", ""),
                "estimate": ev.get("forecast") or "—",
                "previous": ev.get("previous") or "—",
                "actual": actual,
            })
    except Exception as e:
        print(f"[WARN] Yesterday actuals failed: {e}")
    return results

# ---------------------------------------------------------------------------
# 6c. THIS DAY IN HISTORY — Wikipedia free API
# ---------------------------------------------------------------------------
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
        blocks.append(f"TICKER: {p['ticker']} | {p['direction']} | {p['source']}\n{hl}")
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
    if not events:
        return "<p style='color:#666;font-style:italic;'>No US macro releases scheduled.</p>"
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

# ---------------------------------------------------------------------------
# 9. BUILD EMAILS
# ---------------------------------------------------------------------------
def build_daily_email(equity, crypto, macro, gmail, yesterdays, history):
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
        batch_input.append({"ticker": ticker, "direction": direction, "source": source, "headlines": headlines})

    summaries = summarize_all_positions(batch_input, weekly=False)

    # Section numbering
    sec_num = 1
    sections = [gmc_header("Morning Intelligence Brief")]

    # Macro today
    sections.append(section_div(f"&#x2460; Macro Calendar &mdash; Today", macro_table(macro)))
    sec_num += 1

    # Yesterday's actuals
    if yesterdays:
        sections.append(section_div(f"&#x2461; Yesterday&rsquo;s Numbers", yesterdays_table(yesterdays)))
        sec_num += 1

    # This day in history
    if history:
        sections.append(f"""<div style='padding:16px 30px;background:#fdf8f0;border-bottom:1px solid #e8e0d0;'>
  <h3 style='color:#1C3560;font-size:13px;margin:0 0 10px;text-transform:uppercase;letter-spacing:1px;'>&#128197; This Day in History</h3>
  {history_html(history)}
</div>""")
        sec_num += 1

    # Position intelligence
    num_symbol = ["&#x2460;","&#x2461;","&#x2462;","&#x2463;","&#x2464;"][sec_num - 1]
    cards = build_position_cards(equity, crypto, summaries)
    sections.append(section_div(f"{num_symbol} Position Intelligence", cards))

    # Inbox highlights
    if gmail:
        sections.append(f"""<div style='padding:16px 30px;background:#f0f4ff;border-top:1px solid #dce4f5;'>
  <h3 style='color:#1C3560;font-size:13px;margin:0 0 8px;text-transform:uppercase;letter-spacing:1px;'>Inbox Highlights</h3>
  {inbox_html(gmail)}
</div>""")

    sections.append(gmc_footer("Generated"))
    return "\n".join(sections)

def build_weekly_email(equity, crypto, macro, gmail, yesterdays, history):
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

    cards = build_position_cards(equity, crypto, summaries)
    sections.append(section_div("&#x2462; Open Positions &mdash; Weekly Digest", cards))

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
    with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as server:
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

    print("  Fetching this day in history...")
    history = fetch_this_day_in_history()
    print(f"  → {len(history)} history items")

    print("  Fetching Gmail highlights...")
    gmail = fetch_gmail_news(hours=24 if args.mode == "daily" else 120)
    print(f"  → {len(gmail)} relevant emails")

    print("  Summarizing positions (1 API call)...")
    if args.mode == "daily":
        subject = f"GMC Morning Brief — {TODAY_STR}"
        body = build_daily_email(equity, crypto, macro, gmail, yesterdays, history)
    else:
        subject = f"GMC Weekly Close — {TODAY_STR}"
        body = build_weekly_email(equity, crypto, macro, gmail, yesterdays, history)

    print("  Sending email...")
    send_email(subject, body)
    print("[DONE]")

if __name__ == "__main__":
    main()
