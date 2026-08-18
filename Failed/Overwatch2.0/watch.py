#!/usr/bin/env python3
"""
Lahore Road Closure & Protest Watch
------------------------------------
Pulls recent articles from Pakistani news RSS feeds, classifies items as
"protest" or "road closure" based on keyword matches (filtered to mentions
of Lahore), dedupes against previously-posted items, and posts new ones to
a Discord webhook as embeds.

Run on a schedule (e.g. every 6 hours via GitHub Actions / cron).

Why RSS instead of a news-aggregator API:
Google Maps has no public API for road closures or protests, Lahore has no
official open-data traffic feed, and global aggregators (e.g. GDELT) proved
unreliable to call programmatically (aggressive/undocumented rate limiting).
Pulling directly from Pakistani outlets' RSS feeds sidesteps that entirely
-- no key, no rate limit to fight, and better local coverage of Lahore
specifically than a global source would give.

Coverage note:
RSS feeds only carry what these outlets choose to publish. This will catch
anything newsworthy but will still miss purely local reporting that never
makes it onto these feeds, or official Traffic Police announcements that
aren't picked up by a news outlet. See the "Extension point" comment below
for how to merge in a second source (e.g. a Twitter/X feed) later.
"""

import os
import sys
import json
import time
import hashlib
import requests
import feedparser
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
SEEN_CACHE_PATH = os.environ.get("SEEN_CACHE_PATH", "seen_items.json")
MAX_CACHE_ENTRIES = 2000  # trim old entries so the file doesn't grow forever

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml,application/xml,text/xml,*/*",
}

# RSS feeds to pull from. Add/remove URLs here to tune coverage.
# REGIONAL_FEEDS are already scoped to Lahore/Punjab, so we don't require
# the word "Lahore" to appear in the text -- doing so drops relevant items
# that just say e.g. "traffic diverted near Mazang" without naming the city.
# NATIONAL_FEEDS cover all of Pakistan, so we do require a city/nationwide
# signal to avoid pulling in unrelated-city news.
REGIONAL_FEEDS = [
    "https://tribune.com.pk/feed/punjab",
]
NATIONAL_FEEDS = [
    "https://www.dawn.com/feeds/home",
    "https://tribune.com.pk/feed/pakistan",
]
FEEDS = REGIONAL_FEEDS + NATIONAL_FEEDS

# Keyword groups used to classify + filter articles.
PROTEST_KEYWORDS = [
    "protest", "dharna", "sit-in", "rally", "demonstration",
    "shutter down", "strike call", "march", "long march",
]
ROAD_CLOSURE_KEYWORDS = [
    "road closed", "road closure", "route diverted", "traffic diverted",
    "blockade", "highway blocked", "road blocked", "sealed road",
    "container", "containers placed",
]
CITY_TERMS = ["lahore"]
# Nationwide-framed stories (e.g. "JI to hold nationwide protests today")
# are Lahore-relevant even when the city isn't named -- Lahore is Pakistan's
# 2nd-largest city and almost always included in "nationwide" actions.
NATIONWIDE_TERMS = ["nationwide", "across pakistan", "across the country", "all major cities"]

# ---------------------------------------------------------------------------
# Fetching + parsing
# ---------------------------------------------------------------------------

def fetch_feed(url, max_retries=4):
    """Fetch and parse one RSS feed. Retries briefly on transient errors."""
    delay = 5
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
            if resp.status_code == 429:
                if attempt == max_retries:
                    print(f"Rate limited fetching {url}, giving up for this run.",
                          file=sys.stderr)
                    return []
                print(f"429 on {url}, waiting {delay}s (retry {attempt}/{max_retries})...",
                      file=sys.stderr)
                time.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            return parsed.entries or []
        except requests.RequestException as e:
            if attempt == max_retries:
                print(f"Failed to fetch {url}: {e}", file=sys.stderr)
                return []
            time.sleep(delay)
            delay *= 2
    return []


def classify(text, is_regional):
    """Return 'road_closure', 'protest', or None based on keyword matches.

    is_regional=True skips the city-name requirement, since regional feeds
    are already scoped to Lahore/Punjab. For national feeds, we require
    either the city name or a nationwide-framing term, since Lahore is
    almost always included in nationwide actions even when not named.
    """
    lowered = text.lower()
    if not is_regional:
        city_match = any(city in lowered for city in CITY_TERMS)
        nationwide_match = any(term in lowered for term in NATIONWIDE_TERMS)
        if not (city_match or nationwide_match):
            return None
    if any(kw in lowered for kw in ROAD_CLOSURE_KEYWORDS):
        return "road_closure"
    if any(kw in lowered for kw in PROTEST_KEYWORDS):
        return "protest"
    return None


# ---------------------------------------------------------------------------
# Dedup cache
# ---------------------------------------------------------------------------

def item_id(entry):
    link = entry.get("link", "") or entry.get("id", "")
    return hashlib.sha256(link.encode("utf-8")).hexdigest()


def load_seen(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_seen(path, seen):
    if len(seen) > MAX_CACHE_ENTRIES:
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:MAX_CACHE_ENTRIES])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2)


# ---------------------------------------------------------------------------
# Collect new items across all feeds
# ---------------------------------------------------------------------------

def collect_new_items(seen):
    new_items = []
    now_ts = time.time()

    for feed_url in FEEDS:
        is_regional = feed_url in REGIONAL_FEEDS
        entries = fetch_feed(feed_url)
        for entry in entries:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            category = classify(f"{title} {summary}", is_regional)
            if category is None:
                continue

            aid = item_id(entry)
            if aid in seen:
                continue
            seen[aid] = now_ts

            new_items.append({
                "category": category,
                "title": title or "(untitled)",
                "url": entry.get("link", ""),
                "source": feed_url.split("/")[2],  # domain
                "published_parsed": entry.get("published_parsed"),  # time.struct_time or None
            })

        time.sleep(2)  # brief pause between feeds, courteous rather than required

    return new_items


# ---------------------------------------------------------------------------
# Discord posting
# ---------------------------------------------------------------------------

CATEGORY_STYLE = {
    "protest": {"label": "🟠 Protest", "color": 0xE67E22},
    "road_closure": {"label": "🔴 Road Closure", "color": 0xE74C3C},
}


def parse_pub_date(published_parsed):
    if not published_parsed:
        return datetime.now(timezone.utc).isoformat()
    try:
        return datetime(*published_parsed[:6], tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def format_embed(item):
    style = CATEGORY_STYLE[item["category"]]
    return {
        "title": item["title"][:250],
        "url": item["url"],
        "description": f"{style['label']} • {item['source']}",
        "color": style["color"],
        "timestamp": parse_pub_date(item["published_parsed"]),
    }


def post_to_discord(items):
    if not items:
        return
    if not DISCORD_WEBHOOK_URL:
        print("ERROR: DISCORD_WEBHOOK_URL is not set.", file=sys.stderr)
        sys.exit(1)

    for i in range(0, len(items), 10):  # Discord allows max 10 embeds/message
        batch = items[i:i + 10]
        payload = {
            "content": None,
            "embeds": [format_embed(it) for it in batch],
            "username": "Lahore Watch",
        }
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
        if resp.status_code >= 300:
            print(f"Discord post failed ({resp.status_code}): {resp.text}", file=sys.stderr)
        time.sleep(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    seen = load_seen(SEEN_CACHE_PATH)

    all_new = collect_new_items(seen)

    # ---- Extension point: merge in a second source here, e.g. ----
    # all_new += collect_new_items_from_twitter(...)

    dedup = {}
    for item in all_new:
        dedup.setdefault(item["url"], item)
    all_new = list(dedup.values())

    print(f"Found {len(all_new)} new item(s).")
    post_to_discord(all_new)

    save_seen(SEEN_CACHE_PATH, seen)


if __name__ == "__main__":
    main()
    