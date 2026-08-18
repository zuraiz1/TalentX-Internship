#!/usr/bin/env python3
"""
app.py — OVERWATCH local briefing dashboard

Serves your news_briefing.py database as a local webpage.
Run this alongside your hourly fetch/brief schedule (they're independent —
this just reads whatever's already in news.db and the briefings/ folder).

Usage:
    pip install flask
    python app.py
    # then open http://localhost:5000 in your browser

To reach it from your phone on the same network / via Tailscale:
    python app.py --host 0.0.0.0
"""

import argparse
import os
import sys
from datetime import datetime

from flask import Flask, render_template, request, jsonify

# Import the existing news_briefing module — expects it in the same folder
# (or adjust NEWS_BRIEFING_PATH below to point at it).
NEWS_BRIEFING_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, NEWS_BRIEFING_PATH)

import news_briefing as nb  # noqa: E402

app = Flask(__name__)

BRIEFINGS_DIR = os.path.join(NEWS_BRIEFING_PATH, "briefings")

TOPIC_LABELS = ["energy", "business", "security", "politics", "protest", "general"]
CLUSTER_LABELS = ["pakistan_protest", "pakistan_general", "world_conflict"]


def get_latest_saved_briefing():
    """Read the most recently saved briefing_*.txt file, if any exist."""
    if not os.path.isdir(BRIEFINGS_DIR):
        return None, None
    files = [f for f in os.listdir(BRIEFINGS_DIR) if f.startswith("briefing_") and f.endswith(".txt")]
    if not files:
        return None, None
    files.sort(reverse=True)
    latest = files[0]
    with open(os.path.join(BRIEFINGS_DIR, latest), "r", encoding="utf-8") as f:
        content = f.read()
    return latest, content


def relative_time(iso_ts):
    """Turn an ISO timestamp into '12m ago' / '3h ago' style text."""
    try:
        ts = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return iso_ts
    now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
    delta = now - ts
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


@app.route("/")
def index():
    hours = request.args.get("hours", default=12, type=int)
    cluster = request.args.get("cluster", default=None, type=str)
    topic = request.args.get("topic", default=None, type=str)

    conn = nb.init_db()
    clusters = [cluster] if cluster else None
    rows = nb.get_recent_articles(conn, hours=hours, clusters=clusters, topic=topic)
    conn.close()

    items = []
    for r in rows:
        source, cl, title, url, published_at, content, item_topic = r
        items.append({
            "source": source,
            "cluster": cl,
            "title": title,
            "url": url,
            "published_at": published_at,
            "relative": relative_time(published_at),
            "content": content,
            "topic": item_topic or "general",
        })

    briefing_file, briefing_text = get_latest_saved_briefing()

    return render_template(
        "index.html",
        items=items,
        briefing_text=briefing_text,
        briefing_file=briefing_file,
        hours=hours,
        cluster=cluster,
        topic=topic,
        topic_labels=TOPIC_LABELS,
        cluster_labels=CLUSTER_LABELS,
        item_count=len(items),
        now=datetime.now().strftime("%H:%M:%S"),
    )


@app.route("/ask", methods=["POST"])
def ask():
    question = request.form.get("question", "").strip()
    if not question:
        return jsonify({"answer": "Type a question first."})
    conn = nb.init_db()
    answer = nb.answer_question(question, conn)
    conn.close()
    return jsonify({"answer": answer})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False)
