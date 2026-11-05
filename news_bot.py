"""Holt LoL- und Valorant-News und postet neue Artikel per Webhook in Discord.

Läuft einmal durch und beendet sich (wird von GitHub Actions regelmäßig gestartet).
Bereits gepostete Artikel werden in state.json gemerkt.

  python news_bot.py            # normal
  python news_bot.py --dry-run  # nur anzeigen, nichts posten, nichts speichern
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests

import config

STATE_FILE = Path(__file__).with_name("state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; esports-news-discord-bot/1.0)"}
SEEN_LIMIT = 500


# ---------- Hilfsfunktionen ----------

def clean_text(raw: str, limit: int = 300) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", "", title.lower())


def parse_date(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, time.struct_time):
        return datetime(*value[:6], tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------- Quellen ----------

def fetch_rss(url: str) -> list[dict]:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for e in feed.entries:
        title, source = e.get("title", ""), None
        if "news.google.com" in url and " - " in title:  # "Titel - Quelle"
            title, source = title.rsplit(" - ", 1)
        image = None
        for key in ("media_content", "media_thumbnail"):
            if e.get(key):
                image = e[key][0].get("url")
                break
        if not image:
            for enc in e.get("enclosures", []):
                if enc.get("type", "").startswith("image"):
                    image = enc.get("href")
                    break
        items.append({
            "id": e.get("id") or e.get("link"),
            "title": title.strip(),
            "url": e.get("link"),
            "summary": "" if "news.google.com" in url else clean_text(e.get("summary", "")),
            "image": image,
            "date": parse_date(e.get("published_parsed") or e.get("updated_parsed")),
            "source": source or feed.feed.get("title", ""),
        })
    return items


def fetch_riot(url: str) -> list[dict]:
    """Liest die Artikelliste aus den eingebetteten Next.js-Daten der Riot-Seiten."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.S)
    if not m:
        raise ValueError("Keine __NEXT_DATA__ gefunden – Seitenaufbau hat sich geändert?")
    data = json.loads(m.group(1))

    items, seen = [], set()

    def link_of(o: dict):
        for cand in ((o.get("action") or {}).get("payload") or {}).get("url"), o.get("url"), o.get("link"):
            if isinstance(cand, dict):
                cand = cand.get("url")
            if isinstance(cand, str) and cand:
                return urljoin(url, cand)
        return None

    def walk(o):
        if isinstance(o, dict):
            title = o.get("title")
            date = o.get("publishedAt") or o.get("date")
            link = link_of(o)
            if isinstance(title, str) and date and link and link not in seen:
                seen.add(link)
                media = o.get("media") or o.get("banner") or {}
                desc = o.get("description")
                if isinstance(desc, dict):
                    desc = desc.get("body", "")
                items.append({
                    "id": link,
                    "title": title.strip(),
                    "url": link,
                    "summary": clean_text(desc if isinstance(desc, str) else ""),
                    "image": media.get("url") if isinstance(media, dict) else None,
                    "date": parse_date(date),
                    "source": "Riot Games",
                })
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return items


FETCHERS = {"rss": fetch_rss, "riot": fetch_riot}


# ---------- Discord ----------

def post_to_discord(webhook: str, game: dict, src: dict, item: dict):
    embed = {
        "title": item["title"][:256],
        "url": item["url"],
        "color": game["color"],
        "author": {"name": f'{src["label"]} · {game["name"]}'},
        "footer": {"text": item["source"][:100] or "News"},
    }
    if item["summary"]:
        embed["description"] = item["summary"]
    if item["image"]:
        embed["image"] = {"url": item["image"]}
    if item["date"]:
        embed["timestamp"] = item["date"].isoformat()

    for _ in range(5):
        r = requests.post(webhook, json={"embeds": [embed]}, timeout=30)
        if r.status_code == 429:  # Rate-Limit: kurz warten und erneut versuchen
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        time.sleep(1)
        return
    raise RuntimeError("Discord Rate-Limit dauerhaft überschritten")


# ---------- Ablauf ----------

def main(dry_run: bool = False) -> int:
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.MAX_AGE_HOURS)
    excluded = [k.lower() for k in config.EXCLUDE_KEYWORDS]
    errors = 0

    for key, game in config.GAMES.items():
        webhook = os.getenv(game["webhook_env"])
        if not webhook and not dry_run:
            print(f"⚠️  {game['webhook_env']} nicht gesetzt – {game['name']} wird übersprungen.")
            continue
        game_state = state.setdefault(key, {"titles": []})
        seen_titles = set(game_state["titles"])

        for src in game["sources"]:
            seen = game_state.setdefault(src["url"], [])
            first_run = not seen
            try:
                items = FETCHERS[src["type"]](src["url"])
            except Exception as exc:
                errors += 1
                print(f"❌ {game['name']} | {src['url']}: {exc}")
                continue

            new = [i for i in items if i["id"] and i["id"] not in seen]
            fresh = [
                i for i in new
                if (i["date"] is None or i["date"] >= cutoff)
                and norm_title(i["title"]) not in seen_titles
                and not any(k in i["title"].lower() for k in excluded)
            ]
            fresh.sort(key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc))
            # Beim allerersten Lauf nur den neuesten Artikel posten (kein Spam)
            to_post = fresh[-1:] if first_run else fresh[-config.MAX_POSTS_PER_SOURCE:]
            print(f"✅ {game['name']} | {src['label']} | {src['url'][:70]}: "
                  f"{len(items)} gefunden, {len(to_post)} neu")

            for item in to_post:
                if dry_run:
                    print(f"   → {item['title']}  ({item['url']})")
                    continue
                try:
                    post_to_discord(webhook, game, src, item)
                except Exception as exc:
                    errors += 1
                    print(f"❌ Posten fehlgeschlagen: {exc}")
                    break
                seen_titles.add(norm_title(item["title"]))

            # Alles Gefundene als gesehen markieren (auch Übersprungenes)
            ids = [i["id"] for i in items if i["id"]]
            game_state[src["url"]] = (ids + [s for s in seen if s not in ids])[:SEEN_LIMIT]
        game_state["titles"] = list(seen_titles)[-SEEN_LIMIT:]

    if not dry_run:
        STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    return 1 if errors and dry_run else 0


if __name__ == "__main__":
    sys.exit(main(dry_run="--dry-run" in sys.argv))
