#!/usr/bin/env python3
"""
news_briefing.py — Local news gathering + briefing tool

Two retrieval clusters, BOTH pulled via RSS (no API key, no rate limit):
  1. Pakistan protests (Lahore/Islamabad/Karachi) -> direct outlet RSS feeds
  2. Middle East / world conflicts / world politics -> direct outlet RSS feeds
     + per-topic Google News RSS queries (no key required, effectively unlimited)

Everything lands in a single SQLite table, sorted by recency (not by cluster),
then handed to a local Ollama model (default: llama3.1:8b) to write the briefing.
Ollama is used ONLY for local generation/Q&A now -- no web_search API, no key needed.

Usage:
    python news_briefing.py fetch                 # pull fresh news into the DB
    python news_briefing.py brief                  # generate today's briefing from recent items
    python news_briefing.py brief --hours 6         # only use items from the last 6 hours
    python news_briefing.py ask "what's the latest on the Karachi protest?"
    python news_briefing.py list --hours 12         # raw list of stored items, no LLM

Requirements:
    pip install feedparser ollama

Ollama setup:
    ollama pull llama3.1:8b
    # No API key needed anymore -- everything is retrieved via RSS.
"""

import argparse
import difflib
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import feedparser
import ollama

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news.db")
LOCAL_MODEL = "llama3.1:8b"

# Cluster 1: Pakistan protests — RSS feeds (near-real-time, no search API needed)
RSS_FEEDS = [
    ("Dawn - Pakistan", "https://www.dawn.com/feeds/pakistan"),
    ("Dawn - Latest", "https://www.dawn.com/feeds/latest-news"),
    ("Dawn - Business", "https://www.dawn.com/feeds/business"),
    ("The News - National", "https://www.thenews.com.pk/rss/1/1"),
    ("The News - Business", "https://www.thenews.com.pk/rss/1/9"),
    ("Geo News - Pakistan", "https://www.geo.tv/rss/1/2"),
    ("Geo News - Business", "https://www.geo.tv/rss/1/3"),
    ("Business Recorder - Pakistan", "https://www.brecorder.com/feeds/pakistan"),
    ("Business Recorder - Business & Finance", "https://www.brecorder.com/feeds/business-finance"),
    ("Business Recorder - Markets", "https://www.brecorder.com/feeds/markets"),
]

# Keywords used to flag protest-relevant RSS items (case-insensitive substring match)
PROTEST_KEYWORDS = [
    "protest", "rally", "sit-in", "sit in", "dharna", "shutter down",
    "strike", "hartal", "demonstration", "march", "clash", "baton",
    "tear gas", "lockdown", "curfew",
]
PROTEST_CITIES = ["lahore", "islamabad", "karachi"]

# Cluster 2: Middle East / world conflicts — direct outlet RSS feeds
WORLD_RSS_FEEDS = [
    ("Al Jazeera - All", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("BBC - World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC - Middle East", "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml"),
]

# Cluster 2 (supplemental): Google News RSS built per-topic — no API key, no rate limit.
# Google News RSS format: https://news.google.com/rss/search?q=<query>&hl=en-US&gl=US&ceid=US:en
WORLD_SEARCH_TOPICS = [
    "Israel Gaza conflict",
    "Iran news",
    "Syria conflict",
    "Lebanon Hezbollah",
    "Yemen Houthi",
    "Middle East ceasefire negotiations",
    "Russia Ukraine war",
    "Strait of Hormuz shipping",
]


def google_news_rss_url(topic):
    return f"https://news.google.com/rss/search?q={quote_plus(topic)}&hl=en-US&gl=US&ceid=US:en"


# Lightweight topic classifier — first matching category wins, checked in this order.
# Not mutually exclusive with cluster/protest tagging; this is a separate dimension
# so you can later filter e.g. "show me energy-related items regardless of cluster".
TOPIC_KEYWORDS = [
    ("energy", ["oil", "gas", "refinery", "opec", "crude", "lng", "petroleum",
                "pipeline", "electricity", "power plant", "energy"]),
    ("business", ["stock", "market", "rupee", "dollar", "economy", "inflation",
                  "trade", "export", "import", "budget", "tax", "sbp", "imf",
                  "business", "finance", "investment"]),
    ("security", ["militant", "attack", "bomb", "terror", "operation", "army",
                  "security forces", "airstrike", "missile"]),
    ("politics", ["election", "parliament", "minister", "government", "assembly",
                  "senate", "policy", "president", "prime minister"]),
    ("protest", PROTEST_KEYWORDS),
]


def classify_topic(text):
    text_lower = text.lower()
    for topic, keywords in TOPIC_KEYWORDS:
        if any(kw in text_lower for kw in keywords):
            return topic
    return "general"


def normalize_title(title):
    """Strip punctuation/casing so near-identical headlines compare cleanly."""
    cleaned = re.sub(r"[^a-z0-9\s]", "", title.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def is_duplicate(conn, title, cluster, hours=48, threshold=0.82):
    """Fuzzy-match the new title against recent titles in the same cluster.
    Catches the same story appearing across Dawn/Geo/Google News with slightly
    different phrasing, without needing identical URLs."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT title FROM articles WHERE cluster = ? AND published_at >= ?",
        (cluster, cutoff),
    ).fetchall()

    norm_new = normalize_title(title)
    for (existing_title,) in rows:
        norm_existing = normalize_title(existing_title)
        ratio = difflib.SequenceMatcher(None, norm_new, norm_existing).ratio()
        if ratio >= threshold:
            return True
    return False

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            cluster TEXT,           -- 'pakistan_protest' or 'world_conflict'
            topic TEXT,             -- 'energy' / 'business' / 'security' / 'politics' / 'protest' / 'general'
            title TEXT,
            url TEXT UNIQUE,
            published_at TEXT,      -- ISO timestamp, best guess if source doesn't give one
            fetched_at TEXT,
            content TEXT            -- summary/snippet text used for briefing + Q&A grounding
        )
    """)
    # Migration for DBs created before the 'topic' column existed.
    existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(articles)")]
    if "topic" not in existing_cols:
        conn.execute("ALTER TABLE articles ADD COLUMN topic TEXT")
    conn.commit()
    return conn


def save_article(conn, source, cluster, title, url, published_at, content, skip_dedup=False):
    if not skip_dedup and is_duplicate(conn, title, cluster):
        return False  # near-duplicate of something already stored recently

    topic = classify_topic(f"{title} {content}")
    try:
        conn.execute(
            """INSERT OR IGNORE INTO articles
               (source, cluster, topic, title, url, published_at, fetched_at, content)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, cluster, topic, title, url, published_at,
             datetime.now(timezone.utc).isoformat(), content),
        )
        conn.commit()
        return True
    except sqlite3.Error as e:
        print(f"  [warn] failed to save '{title[:60]}': {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Cluster 1: RSS fetch (Pakistan protests)
# ---------------------------------------------------------------------------

def fetch_rss(conn):
    print("Fetching Pakistani RSS feeds...")
    saved, skipped = 0, 0
    for source_name, feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"  [warn] could not fetch {source_name}: {e}", file=sys.stderr)
            continue

        for entry in feed.entries:
            title = entry.get("title", "")
            summary = entry.get("summary", "") or entry.get("description", "")
            link = entry.get("link", "")
            if not title or not link:
                continue

            # Prefer the feed's own published time; fall back to "just fetched"
            if entry.get("published_parsed"):
                published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
            else:
                published_at = datetime.now(timezone.utc).isoformat()

            text_blob = f"{title} {summary}".lower()
            is_protest = any(k in text_blob for k in PROTEST_KEYWORDS)
            is_target_city = any(c in text_blob for c in PROTEST_CITIES) or "pakistan" in text_blob

            # Keep general Pakistan headlines too (cheap to store), but tag protest-relevant ones clearly
            cluster = "pakistan_protest" if (is_protest and is_target_city) else "pakistan_general"

            if save_article(conn, source_name, cluster, title, link, published_at, summary):
                saved += 1
            else:
                skipped += 1

    print(f"  done. {saved} new, {skipped} skipped (duplicate or already stored).")


# ---------------------------------------------------------------------------
# Cluster 2: RSS fetch (Middle East / world conflicts)
# ---------------------------------------------------------------------------

def fetch_world_rss(conn):
    print("Fetching world/Middle East RSS feeds...")
    stats = {"saved": 0, "skipped": 0}

    # Direct outlet feeds
    for source_name, feed_url in WORLD_RSS_FEEDS:
        _fetch_one_world_feed(conn, source_name, feed_url, stats)

    # Per-topic Google News RSS (acts like a search query, but is just RSS)
    for topic in WORLD_SEARCH_TOPICS:
        feed_url = google_news_rss_url(topic)
        _fetch_one_world_feed(conn, f"Google News: {topic}", feed_url, stats)

    print(f"  done. {stats['saved']} new, {stats['skipped']} skipped (duplicate or already stored).")


def _fetch_one_world_feed(conn, source_name, feed_url, stats):
    try:
        feed = feedparser.parse(feed_url)
    except Exception as e:
        print(f"  [warn] could not fetch {source_name}: {e}", file=sys.stderr)
        return

    for entry in feed.entries:
        title = entry.get("title", "")
        summary = entry.get("summary", "") or entry.get("description", "")
        link = entry.get("link", "")
        if not title or not link:
            continue

        if entry.get("published_parsed"):
            published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
        else:
            published_at = datetime.now(timezone.utc).isoformat()

        if save_article(conn, source_name, "world_conflict", title, link, published_at, summary[:2000]):
            stats["saved"] += 1
        else:
            stats["skipped"] += 1


# ---------------------------------------------------------------------------
# Retrieval for briefing / Q&A
# ---------------------------------------------------------------------------

def get_recent_articles(conn, hours=12, clusters=None, topic=None):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    query = "SELECT source, cluster, title, url, published_at, content, topic FROM articles WHERE published_at >= ?"
    params = [cutoff]
    if clusters:
        placeholders = ",".join("?" for _ in clusters)
        query += f" AND cluster IN ({placeholders})"
        params.extend(clusters)
    if topic:
        query += " AND topic = ?"
        params.append(topic)
    query += " ORDER BY published_at DESC"
    rows = conn.execute(query, params).fetchall()
    return rows


def search_articles(conn, keyword, limit=15):
    like = f"%{keyword}%"
    rows = conn.execute(
        """SELECT source, cluster, title, url, published_at, content, topic
           FROM articles
           WHERE title LIKE ? OR content LIKE ?
           ORDER BY published_at DESC LIMIT ?""",
        (like, like, limit),
    ).fetchall()
    return rows


STOPWORDS = {
    "what's", "whats", "what", "is", "the", "in", "on", "at", "a", "an", "of",
    "happening", "going", "right", "now", "today", "with", "to", "and", "or",
    "latest", "news", "about", "tell", "me", "give", "please", "current",
    "situation", "update", "updates", "for", "are", "was", "were", "be",
}


def extract_keywords(question):
    words = [w.strip(".,?!'\"").lower() for w in question.split()]
    keywords = [w for w in words if w and w not in STOPWORDS and len(w) > 2]
    return keywords or words


def search_by_keywords(conn, question, limit=15):
    """Search stored articles using each meaningful keyword from the question,
    merging and de-duplicating results (most-matched / most-recent first)."""
    keywords = extract_keywords(question)
    seen = {}
    for kw in keywords:
        for row in search_articles(conn, kw, limit=limit):
            url = row[3]
            if url not in seen:
                seen[url] = [row, 1]
            else:
                seen[url][1] += 1  # matched more than one keyword -> more relevant
    ranked = sorted(seen.values(), key=lambda x: (x[1], x[0][4]), reverse=True)
    return [r[0] for r in ranked[:limit]]


# ---------------------------------------------------------------------------
# LLM calls (local Ollama model)
# ---------------------------------------------------------------------------

def generate_briefing(rows, model=LOCAL_MODEL):
    if not rows:
        return "No recent articles found in the given time window. Try running `fetch` first, or widen --hours."

    source_block = "\n\n".join(
        f"[{i+1}] ({r[4]}) {r[2]}\nSource: {r[0]} | Cluster: {r[1]}\nURL: {r[3]}\nContent: {r[5]}"
        for i, r in enumerate(rows)
    )

    prompt = f"""You are writing a recency-sorted news briefing for a reader based in Pakistan
who follows Middle East/world conflict news and Pakistani protest news (Lahore, Islamabad, Karachi).

Below are raw news items, sorted newest first, pulled from RSS and web search.
Write a single chronological briefing (most recent first) covering the genuinely newsworthy items.
Do not fabricate details not present in the source text. Skip duplicate/near-duplicate stories.
For each item give: a short headline-style line, 1-3 sentence summary, and cite the item number in brackets.
Group nothing by topic — keep strict recency order across both Pakistan and world items.

SOURCE ITEMS:
{source_block}

Write the briefing now."""

    response = ollama.generate(model=model, prompt=prompt)
    return response.get("response", "").strip()


def answer_question(question, conn, model=LOCAL_MODEL):
    rows = search_by_keywords(conn, question)
    if not rows:
        # fall back to broader recent context if keyword search finds nothing
        rows = get_recent_articles(conn, hours=24)

    if not rows:
        return "Nothing relevant found in the local database. Run `fetch` first."

    source_block = "\n\n".join(
        f"[{i+1}] ({r[4]}) {r[2]}\nSource: {r[0]}\nURL: {r[3]}\nContent: {r[5]}"
        for i, r in enumerate(rows)
    )

    prompt = f"""Answer the user's question using ONLY the source items below. If the sources don't
contain the answer, say so plainly instead of guessing. Cite item numbers in brackets.

SOURCE ITEMS:
{source_block}

QUESTION: {question}

ANSWER:"""

    response = ollama.generate(model=model, prompt=prompt)
    return response.get("response", "").strip()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Local news gathering + briefing tool")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("fetch", help="Pull fresh news into the local database")

    p_brief = sub.add_parser("brief", help="Generate a recency-sorted briefing")
    p_brief.add_argument("--hours", type=int, default=12, help="How far back to pull articles from")
    p_brief.add_argument("--save", type=str, default=None,
                          help="Directory to save the briefing as a timestamped .txt file")

    p_ask = sub.add_parser("ask", help="Ask a follow-up question grounded in stored articles")
    p_ask.add_argument("question", type=str)

    p_list = sub.add_parser("list", help="List raw stored articles (no LLM call)")
    p_list.add_argument("--hours", type=int, default=12)
    p_list.add_argument("--topic", type=str, default=None,
                         help="Filter by topic: energy, business, security, politics, protest, general")

    args = parser.parse_args()
    conn = init_db()

    if args.command == "fetch":
        fetch_rss(conn)
        fetch_world_rss(conn)

    elif args.command == "brief":
        rows = get_recent_articles(conn, hours=args.hours)
        header = f"\n--- Briefing ({len(rows)} items, last {args.hours}h) ---\n"
        body = generate_briefing(rows)
        print(header)
        print(body)
        if args.save:
            os.makedirs(args.save, exist_ok=True)
            fname = datetime.now().strftime("briefing_%Y-%m-%d_%H-%M.txt")
            path = os.path.join(args.save, fname)
            with open(path, "w", encoding="utf-8") as f:
                f.write(header + "\n" + body)
            print(f"\n[saved to {path}]")

    elif args.command == "ask":
        print(answer_question(args.question, conn))

    elif args.command == "list":
        rows = get_recent_articles(conn, hours=args.hours, topic=args.topic)
        for r in rows:
            print(f"[{r[1]:16}] [{r[6] or 'general':9}] {r[4]}  {r[2][:70]}  ({r[0]})")
        print(f"\n{len(rows)} items in the last {args.hours}h")

    conn.close()


if __name__ == "__main__":
    main()
