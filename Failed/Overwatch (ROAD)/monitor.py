"""
Pakistan Road Blockade Monitor
--------------------------------
Polls a set of sources (news RSS tags, Nitter mirrors of official accounts),
uses a local Ollama LLM pass to filter + extract structured blockade events,
dedups against a local SQLite store, and pushes new/changed events to Discord
via a webhook.

Run modes:
  python monitor.py          -> single pass (good for cron/Task Scheduler)
  python monitor.py --loop   -> runs forever, polls every POLL_INTERVAL_SEC
  python monitor.py --debug  -> shows every candidate text + accept/reject reason
"""

import json
import sqlite3
import time
import hashlib
import argparse
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# CONFIG — fill these in
# ---------------------------------------------------------------------------

OLLAMA_URL = "http://localhost:11434/api/generate"  # default Ollama endpoint
OLLAMA_MODEL = "llama3.1:8b"

# Leave this blank to run without Discord for now — alerts will just print
# to console. Fill it in later whenever you set up the webhook; no code
# changes needed elsewhere.
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1536664953107382292/AToBfbroAQEgdMiFWKjauKEoIxtx4NHM1tb5bSO46oV3eiiuL-fUZOyM_w99bXjP0hKX"

DB_PATH = "blockades.db"
POLL_INTERVAL_SEC = 900   # 15 min

# Ignore articles older than this — Google News resurfaces old pieces, and a
# blockade reported 3 days ago is very likely already resolved.
MAX_ARTICLE_AGE_HOURS = 20

STATUSES = ["blocked", "partial", "upcoming", "cleared"]

# Add / remove sources here. Each is just "give me raw text to scan".
#
# Google News RSS search: no per-site feed needed, aggregates across all
# indexed Pakistani outlets for a given query. Keep queries narrow and
# city/keyword-specific rather than one broad query — broad queries return
# more general/stale results.
#
# IMPORTANT: plain keyword queries rank by relevance, not recency — Google
# will happily surface a big protest from 2022 over a smaller one from
# yesterday. Appending "when:Xd" restricts results server-side to the last
# X days, which matters far more than any client-side filtering.
def google_news_rss(query, region="PK", when_days=2):
    from urllib.parse import quote
    q = quote(f"{query} when:{when_days}d")
    return f"https://news.google.com/rss/search?q={q}&hl=en-{region}&gl={region}&ceid={region}:en"

SOURCES = [
    {"type": "rss", "name": "GNews - Lahore roads",
     "url": google_news_rss("road blocked OR road closed Lahore")},
    {"type": "rss", "name": "GNews - Karachi roads",
     "url": google_news_rss("road blocked OR road closed Karachi")},
    {"type": "rss", "name": "GNews - Islamabad roads",
     "url": google_news_rss("road blocked OR road closed Islamabad")},
    {"type": "rss", "name": "GNews - Pakistan protest highway",
     "url": google_news_rss('"Pakistan" protest highway blocked -India -UK -Nigeria')},

    # Nitter mirror of official accounts (swap NITTER_INSTANCE if one dies)
    {"type": "nitter", "name": "NHMP",           "handle": "NHMPofficial"},
    {"type": "nitter", "name": "Punjab Police",  "handle": "PunjabPoliceOfficial"},
    {"type": "nitter", "name": "Islamabad Traffic Police", "handle": "ITPofficial"},
]

NITTER_INSTANCE = "https://nitter.net"  # replace if instance is down

CATEGORIES = ["protest", "accident", "disaster", "construction",
              "security/VIP movement", "celebration/procession", "other"]

EXTRACTION_PROMPT = """You are a filter+extractor for a Pakistan road-blockage monitor.
Today's date is {today}.

Given a piece of raw text (a news snippet), decide whether it describes a
PHYSICAL ROAD in PAKISTAN that IS currently blocked, IS partially blocked, or
WILL be blocked soon (an announced/planned future closure, e.g. "roads will
be closed tomorrow for the rally").

Reject (respond is_blockage: false) if:
- the location is not in Pakistan (even if it mentions "Pakistan" elsewhere
  in the text, e.g. a wire-service dateline from another country)
- it's about electricity/gas/water SUPPLY outages, feeder shutdowns, or
  utility maintenance schedules — these are NOT road blockages even if they
  mention "closure" or involve infrastructure work, unless the text also
  explicitly says a road/street/highway was physically closed as a result
- the text is only about a blockage that has already ended/reopened, UNLESS
  the point of the text is announcing the reopening (in which case set
  status to "cleared" so we can update our records — still respond
  is_blockage: true in that case)
- the text is general commentary, politics, or analysis with no concrete
  road/location mentioned
- the text refers to a blockage from more than a day or two in the past with
  no indication it's ongoing
- the match is coincidental keyword overlap (e.g. a property listing
  mentioning "Blocks" as in housing plot blocks, not road blocks)

Respond with ONLY this JSON (no markdown, no preamble):
{{"is_blockage": true, "road_or_area": "actual road/highway name if known, else area name", "city": "the specific city/town if mentioned, otherwise 'Nationwide' for general country-wide notices — never leave this blank", "reason": "one of {categories}", "status": "one of {statuses}", "summary": "one short sentence including WHEN if status is upcoming"}}

Or if it doesn't qualify:
{{"is_blockage": false}}

Text:
\"\"\"{text}\"\"\"
"""

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            road_or_area TEXT,
            city TEXT,
            reason TEXT,
            status TEXT,
            summary TEXT,
            source TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.commit()
    return conn


def event_id(ev):
    # Same road+city+reason within a rolling window = same event.
    key = f"{ev['city'].lower()}|{ev['road_or_area'].lower()}|{ev['reason'].lower()}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def upsert_event(conn, ev, source):
    eid = event_id(ev)
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute("SELECT status FROM events WHERE id=?", (eid,)).fetchone()

    if existing is None:
        conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, ev["road_or_area"], ev["city"], ev["reason"], ev["status"],
             ev["summary"], source, now, now)
        )
        conn.commit()
        return "new"
    else:
        old_status = existing[0]
        conn.execute(
            "UPDATE events SET status=?, summary=?, last_seen=? WHERE id=?",
            (ev["status"], ev["summary"], now, eid)
        )
        conn.commit()
        return "updated" if old_status != ev["status"] else "seen"


# ---------------------------------------------------------------------------
# FETCHING
# ---------------------------------------------------------------------------

def fetch_rss(url, debug=False):
    import feedparser
    from time import mktime
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0 Safari/537.36",
               "Accept": "application/rss+xml, application/xml, text/xml"}

    # Fetch with requests instead of letting feedparser handle the network
    # call — feedparser fails silently on bad responses (redirects, consent
    # pages, rate limits), whereas this way we can see exactly what happened.
    resp = requests.get(url, headers=headers, timeout=15)
    if debug:
        print(f"[debug] status={resp.status_code} len={len(resp.content)} "
              f"content-type={resp.headers.get('content-type')}")
        print(f"[debug] first 300 chars: {resp.text[:300]!r}")

    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    if debug:
        print(f"[debug] bozo={feed.bozo} bozo_exception={feed.get('bozo_exception')}")
        print(f"[debug] entries found: {len(feed.entries)}")

    now = datetime.now(timezone.utc)

    # Google News RSS sorts by relevance, not recency — the top 20 entries
    # can easily all be old "evergreen" articles while genuinely recent ones
    # sit further down. So: parse dates for everything first, sort newest
    # first, then apply the age filter.
    dated_entries = []
    for e in feed.entries:
        published = e.get("published_parsed")
        pub_dt = datetime.fromtimestamp(mktime(published), tz=timezone.utc) if published else None
        dated_entries.append((pub_dt, e))
    dated_entries.sort(key=lambda x: x[0] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    items = []
    skipped_old = 0
    for pub_dt, e in dated_entries:
        if len(items) >= 20:
            break
        text = f"{e.get('title','')}. {e.get('summary','')}"
        if pub_dt:
            age_hours = (now - pub_dt).total_seconds() / 3600
            if age_hours > MAX_ARTICLE_AGE_HOURS:
                skipped_old += 1
                continue
        items.append(text)
    if debug:
        print(f"[debug] kept {len(items)}, skipped {skipped_old} for age "
              f"(out of {len(feed.entries)} total entries)")
    return items


def fetch_nitter(handle):
    url = f"{NITTER_INSTANCE}/{handle}/rss"
    try:
        return fetch_rss(url)
    except Exception:
        return []


def fetch_all_texts():
    texts = []
    for src in SOURCES:
        try:
            if src["type"] == "rss":
                items = fetch_rss(src["url"])
            elif src["type"] == "nitter":
                items = fetch_nitter(src["handle"])
            else:
                items = []
            for t in items:
                texts.append((t, src["name"]))
        except Exception as e:
            print(f"[warn] failed to fetch {src['name']}: {e}")
    return texts


# ---------------------------------------------------------------------------
# LLM EXTRACTION (local Ollama, llama3.1:8b)
# ---------------------------------------------------------------------------

def call_ollama(prompt):
    r = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0},
        },
        timeout=60,  # local 8B inference is slower than an API call, give it room
    )
    r.raise_for_status()
    return r.json()["response"]


def extract_event(text, debug=False):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = EXTRACTION_PROMPT.format(
        categories=", ".join(CATEGORIES), statuses="|".join(STATUSES),
        today=today, text=text[:1500]
    )
    try:
        raw = call_ollama(prompt)
    except Exception as e:
        print(f"[error] Ollama call failed ({e}). Is `ollama serve` running "
              f"and is {OLLAMA_MODEL} pulled?")
        return None

    if debug:
        print(f"[debug] raw model output: {raw!r}")

    raw = raw.strip().strip("`").strip()
    if raw.lower().startswith("json"):
        raw = raw[4:].strip()
    try:
        data = json.loads(raw)
    except Exception as e:
        if debug:
            print(f"[debug] JSON parse failed: {e}")
        return None

    if not data.get("is_blockage"):
        if debug:
            print(f"[debug] rejected: is_blockage=false")
        return None
    required = ["road_or_area", "city", "reason", "status", "summary"]
    if not all(k in data for k in required):
        if debug:
            print(f"[debug] rejected: missing required field(s), got keys={list(data.keys())}")
        return None
    for field in ["road_or_area", "city", "summary"]:
        val = str(data.get(field, "")).strip().lower()
        if val in ("", "n/a", "unknown", "none", "not mentioned", "not specified"):
            if debug:
                print(f"[debug] rejected: blank/placeholder '{field}' = {data.get(field)!r}")
            return None
    return data


# ---------------------------------------------------------------------------
# DISCORD PUSH
# ---------------------------------------------------------------------------

def send_discord(message):
    if not DISCORD_WEBHOOK_URL:
        # Discord not set up yet — just log so you can still see alerts
        # while testing the fetch/extraction pipeline.
        print(f"[alert - discord not configured]\n{message}\n")
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    except Exception as e:
        print(f"[warn] Discord push failed: {e}")


def format_alert(ev, kind):
    status_icon = {"blocked": "🚧", "partial": "⚠️", "upcoming": "🕒"}.get(ev["status"], "ℹ️")
    kind_label = {"new": "NEW", "updated": "UPDATE"}.get(kind, "")
    return (f"{status_icon} **{kind_label}** — {ev['city']}: {ev['road_or_area']}\n"
            f"Status: {ev['status']}\n"
            f"Reason: {ev['reason']}\n"
            f"{ev['summary']}")


# ---------------------------------------------------------------------------
# MAIN PASS
# ---------------------------------------------------------------------------

def run_once(conn, debug=False):
    texts = fetch_all_texts()
    print(f"[info] fetched {len(texts)} candidate texts (after age filter)")
    for text, source in texts:
        if debug:
            print(f"\n[debug] --- checking text from {source} ---")
            print(f"[debug] text: {text[:200]!r}")
        ev = extract_event(text, debug=debug)
        if ev is None:
            continue
        kind = upsert_event(conn, ev, source)
        if ev["status"] == "cleared":
            # Keep the record accurate for future dedup, but you don't need
            # a ping for roads that are already open again.
            if kind != "seen":
                print(f"[cleared, no alert] {ev['city']} / {ev['road_or_area']} ({source})")
            continue
        if kind in ("new", "updated"):
            send_discord(format_alert(ev, kind))
            print(f"[{kind}] {ev['city']} / {ev['road_or_area']} ({source})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="run continuously")
    parser.add_argument("--debug", action="store_true",
                         help="show every candidate text and why it was accepted/rejected")
    args = parser.parse_args()

    conn = init_db()

    if args.loop:
        while True:
            run_once(conn, debug=args.debug)
            time.sleep(POLL_INTERVAL_SEC)
    else:
        run_once(conn, debug=args.debug)
