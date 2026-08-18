# Lahore Blockade Watch

Posts new **road closure** and **protest** news for Lahore to a Discord
channel, every 6 hours, via a GitHub Actions cron job.

## How it works

1. Every 6 hours, `watch.py` pulls recent articles from a set of Pakistani
   news RSS feeds (Dawn, Express Tribune Punjab/Pakistan sections).
2. Each article's title + summary is checked for a mention of Lahore plus
   a road-closure or protest keyword.
3. New matching articles (not seen in a previous run) are posted to your
   Discord channel as embeds, tagged 🔴 Road Closure or 🟠 Protest.
4. A small JSON file (`seen_items.json`) tracks which article URLs have
   already been posted, so nothing repeats. This file is committed back
   to the repo automatically after each run.

## Why RSS instead of an API

The first version of this used Google's/GDELT's news-aggregator API, but
it turned out to rate-limit or block plain `requests`-library traffic
inconsistently (an undocumented anti-bot measure, not a documented quota).
Pulling directly from the outlets' own RSS feeds sidesteps that entirely —
no key, no rate limit to fight — and gives better Lahore-specific coverage
than a global aggregator would anyway.

## Known limitation (read this)

Google Maps has no public API for road closures or protests, and Lahore
has no official open-data traffic feed. These RSS feeds only carry what
these outlets choose to publish, so this will catch anything newsworthy
but will **miss** purely local reporting that never makes these feeds, and
official Traffic Police announcements that aren't picked up by a news
outlet.

**To improve accuracy later:** the Lahore Traffic Police often post
closures directly on X/Twitter. If you get API access to that account's
feed, there's a clear extension point in `watch.py` (search for
"Extension point") where you can merge a second source in before the
dedup/posting step.

## Setup

1. **Create a Discord webhook** (skip if you already have one):
   Server Settings → Integrations → Webhooks → New Webhook → copy the URL.

2. **Push this folder to a new GitHub repo.**

3. **Add the webhook as a secret:**
   Repo → Settings → Secrets and variables → Actions → New repository secret
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: your webhook URL

4. **Enable Actions** if prompted (Actions tab → "I understand, enable").

5. That's it — it will run automatically every 6 hours. To test it
   immediately, go to Actions → "Lahore Blockade Watch" → "Run workflow".

## Running locally (optional, for testing)

```bash
pip install -r requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python watch.py
```

## Tuning

- **Feeds**: edit the `FEEDS` list in `watch.py` to add or remove RSS
  sources. Any standard RSS/Atom feed URL works.
- **Keywords**: edit `PROTEST_KEYWORDS` / `ROAD_CLOSURE_KEYWORDS` in
  `watch.py` to narrow or broaden matches.
- **Other cities**: edit `CITY_TERMS` — the script isn't Lahore-specific
  in its logic, just its default filter (and you'd likely want to swap in
  feeds relevant to that city too).
- **Frequency**: edit the `cron` line in `.github/workflows/watch.yml`.
  `0 */6 * * *` = every 6 hours at :00.
