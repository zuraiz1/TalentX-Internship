# Overview

NewsWatch is a local, private news-briefing tool. It scrapes your configured RSS feeds, filters them by region and topic using a local LLM (llama3.1:8b via Ollama), tracks specific topics (wars, protests, marches...) with automatic expiry, and produces a formatted PDF brief divided into Local News, International News, and a Watchlist section. Nothing leaves your machine except the RSS fetches themselves. It also has a completely separate, LLM-free markets tracker for KSE-100, crude, and fuel prices.

---

# How to use

Firstly, [Set up](#Set%20up) the system. Once set up:

1. Run a brief whenever you want one:

```bash
python main.py run
```

This scrapes, dedupes, filters, classifies, and writes a PDF to `./briefs/brief_YYYY-MM-DD.pdf`. Articles already used in a past brief won't repeat in future ones.
1. Or use the interactive shell for a nicer experience (type `/` to see command suggestions):

```bash
python main.py shell
```

1. Manage the watchlist as topics come up (topics get their own highlighted section and bypass the region filter):

```bash
python main.py watchlist add "Sudan civil war" --keywords Sudan,RSF,Khartoum,SAF --category war --ttl 21 --priority 3python main.py watchlist listpython main.py watchlist renew 1 --ttl 14python main.py watchlist remove 1
```

1. Optionally check markets on their own schedule:

```bash
python main.py markets-check
```


## Tips

- If you don't want to run things manually every day, add the cron entries under [Automating it](#automating-it-optional) - the brief and markets-check run independently of each other.
- Run `markets-check` once and check the printed values before trusting it, especially fuel prices - the OGRA/PSO page has no stable public API, so its scraping pattern can silently stop matching. It logs a warning and skips that one ticker rather than storing a bad number.
- If old news is creeping into a brief, check `/status` or the run output's "dropped: N stale" count first - it's usually a feed serving backlog items, not a bug.
- If unrelated stories are getting merged into one cluster, raise `dedup.jaccard_threshold` in `config.yaml`; if obvious duplicates aren't being caught, lower it.
- Watchlist topics expire automatically on their TTL - that's what keeps the LLM's per-cycle workload from growing without bound, so renew anything still relevant rather than re-adding it.
- If you've never run `markets-check`, the Markets section just won't appear in the PDF - no error, no blank space.

---

# Set up

|Step|What to do|
|---|---|
|1. Install Ollama|Install [Ollama](https://ollama.com), then `ollama pull llama3.1:8b` and `ollama serve` (if not already running as a service).|
|2. Install dependencies|`pip install -r requirements.txt --break-system-packages`|
|3. Configure sources|Open `config.yaml` and set `sources` (RSS feeds), `regions.include` (leave empty to accept all regions), `exclusions.keywords`, and `llm.model` if you're using something other than llama3.1:8b.|
|4. Tag scope per source|Make sure each source in `config.yaml` is tagged `scope: local` or `scope: international` based on the _feed's_ content focus, not the outlet's home country.|
|5. Test it|Run `python main.py run` and check `./briefs/` for the generated PDF, and the console output for any dropped/error counts.|
|6. Done|Run `python main.py run` / `markets-check` manually, or set up cron (below) for hands-off use.|

## Automating it (optional)

```bash
# Daily brief at 7am
0 7 * * * cd /path/to/newswatch && python3 main.py run >> run.log 2>&1

# Markets, every few hours (match markets.update_times in config.yaml)
0 10,13,16 * * * cd /path/to/newswatch && python3 main.py markets-check >> markets.log 2>&1
```

## Files

|File|Purpose|
|---|---|
|`config.yaml`|Everything you're likely to want to tune|
|`main.py`|CLI - `run`, `watchlist`, `markets-check`|
|`newswatch.db`|Created on first run - your article + market history|
|`briefs/`|Generated PDFs land here|