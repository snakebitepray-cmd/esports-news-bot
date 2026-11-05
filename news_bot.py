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
from urllib.parse import urljoin, urlparse

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
    site = urlparse(url).netloc.removeprefix("www.")

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
            # nur echte Artikel der Riot-Seite (keine Instagram-/YouTube-/X-Posts)
            on_site = link and urlparse(link).netloc.removeprefix("www.").endswith(site)
            if isinstance(title, str) and date and on_site and link not in seen:
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


def score_line(a: str, sa, b: str, sb) -> str:
    score = f"{sa} - {sb}"
    if config.SPOILER_RESULTS:
        return f"{a} ||{score}|| {b}"
    if str(sa).isdigit() and str(sb).isdigit():  # Sieger fett
        if int(sa) > int(sb):
            a = f"**{a}**"
        elif int(sb) > int(sa):
            b = f"**{b}**"
    return f"{a} {score} {b}"


# Öffentlicher Schlüssel, den auch die Website lolesports.com selbst verwendet
LOLESPORTS_KEY = "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"


def fetch_lolesports(src: dict) -> list[dict]:
    """Abgeschlossene Matches der gewählten Ligen von lolesports.com."""
    api = "https://esports-api.lolesports.com/persisted/gw"
    hdr = {**HEADERS, "x-api-key": LOLESPORTS_KEY}
    leagues = requests.get(f"{api}/getLeagues", params={"hl": "en-US"}, headers=hdr, timeout=30)
    leagues.raise_for_status()
    wanted = [lg for lg in leagues.json()["data"]["leagues"]
              if any(lg["slug"].startswith(s) for s in src["leagues"])]
    if not wanted:
        raise ValueError("Keine der gewünschten Ligen gefunden")
    sched = requests.get(f"{api}/getSchedule", headers=hdr, timeout=30,
                         params={"hl": "en-US", "leagueId": ",".join(lg["id"] for lg in wanted)})
    sched.raise_for_status()

    items = []
    for ev in sched.json()["data"]["schedule"]["events"]:
        match = ev.get("match") or {}
        teams = match.get("teams") or []
        if ev.get("state") != "completed" or len(teams) != 2:
            continue
        t1, t2 = teams
        w1 = (t1.get("result") or {}).get("gameWins", 0)
        w2 = (t2.get("result") or {}).get("gameWins", 0)
        winner = t1 if w1 > w2 else t2
        league = (ev.get("league") or {}).get("name", "")
        info = [x for x in (league, ev.get("blockName")) if x]
        if (match.get("strategy") or {}).get("count"):
            info.append(f'Bo{match["strategy"]["count"]}')
        items.append({
            "id": f'lol-{match.get("id")}',
            "title": f"{t1['name']} vs {t2['name']}",
            "url": None,
            "summary": score_line(t1.get("code") or t1["name"], w1, t2.get("code") or t2["name"], w2)
                       + "\n" + " · ".join(info),
            "image": None,
            "thumbnail": None if config.SPOILER_RESULTS else winner.get("image"),
            "date": parse_date(ev.get("startTime")),
            "source": "lolesports.com",
            "is_result": True,
        })
    return items


def parse_ago(text: str) -> datetime | None:
    """'2h 30m ago' -> Zeitpunkt."""
    units = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
    parts = re.findall(r"(\d+)\s*([wdhms])", text or "")
    if not parts:
        return None
    return datetime.now(timezone.utc) - timedelta(seconds=sum(int(n) * units[u] for n, u in parts))


def fetch_vlr(src: dict) -> list[dict]:
    """Abgeschlossene Matches von vlr.gg (nur gewünschte Events)."""
    from bs4 import BeautifulSoup

    resp = requests.get(src["url"], headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    matches = soup.select("a.match-item")
    if not matches:  # Seite ganz ohne Matches = Aufbau geändert oder blockiert
        raise ValueError("Keine Matches auf vlr.gg gefunden – Seitenaufbau geändert?")
    print(f"   (vlr.gg: {len(matches)} Matches auf der Seite, davon VCT werden gefiltert)")
    items = []
    for a in matches:
        ev_el = a.select_one(".match-item-event")
        series_el = a.select_one(".match-item-event-series")
        series = series_el.get_text(" ", strip=True) if series_el else ""
        if series_el:
            series_el.extract()
        event = ev_el.get_text(" ", strip=True) if ev_el else ""
        if not any(k in event.lower() for k in src["events"]):
            continue
        teams = a.select(".match-item-vs-team")
        if len(teams) != 2:
            continue
        names = [t.select_one(".match-item-vs-team-name").get_text(" ", strip=True) for t in teams]
        scores = [t.select_one(".match-item-vs-team-score").get_text(strip=True) for t in teams]
        eta = a.select_one(".ml-eta")
        items.append({
            "id": "vlr-" + a["href"].split("/")[1],
            "title": f"{names[0]} vs {names[1]}",
            "url": urljoin(src["url"], a["href"]),
            "summary": score_line(names[0], scores[0], names[1], scores[1])
                       + "\n" + " · ".join(x for x in (event, series) if x),
            "image": None,
            "date": parse_ago(eta.get_text(" ", strip=True)) if eta else None,
            "source": "vlr.gg",
            "is_result": True,
        })
    return items


FETCHERS = {
    "rss": lambda src: fetch_rss(src["url"]),
    "riot": lambda src: fetch_riot(src["url"]),
    "lolesports": fetch_lolesports,
    "vlr": fetch_vlr,
}


# ---------- Discord ----------

def label_for(src: dict, item: dict) -> str:
    """Esports-Artikel über Spielerwechsel bekommen das Transfer-Label."""
    title = item["title"].lower()
    if "Esports" in src["label"] and any(w in title for w in config.TRANSFER_WORDS):
        return config.TRANSFER_LABEL
    return src["label"]


def post_to_discord(webhook: str, game: dict, src: dict, item: dict):
    embed = {
        "title": item["title"][:256],
        "color": game["color"],
        "author": {"name": f'{label_for(src, item)} · {game["name"]}'},
        "footer": {"text": item["source"][:100] or "News"},
    }
    if item["summary"]:
        embed["description"] = item["summary"]
    if item["url"]:
        embed["url"] = item["url"]
    if item["image"]:
        embed["image"] = {"url": item["image"]}
    if item.get("thumbnail"):
        embed["thumbnail"] = {"url": item["thumbnail"]}
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
                items = FETCHERS[src["type"]](src)
            except Exception as exc:
                errors += 1
                print(f"❌ {game['name']} | {src['url']}: {exc}")
                continue

            new = [i for i in items if i["id"] and i["id"] not in seen]
            fresh = [
                i for i in new
                if (i["date"] is None or i["date"] >= cutoff)
                # gleiche Story aus mehreren Quellen nur einmal (nicht bei Ergebnissen)
                and (i.get("is_result") or norm_title(i["title"]) not in seen_titles)
                and not any(k in i["title"].lower() for k in excluded)
                and (not src.get("title_must_contain")
                     or any(k in i["title"].lower() for k in src["title_must_contain"]))
            ]
            fresh.sort(key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc))
            # Beim allerersten Lauf nur den neuesten Artikel posten (kein Spam)
            to_post = fresh[-1:] if first_run else fresh[-config.MAX_POSTS_PER_SOURCE:]
            print(f"✅ {game['name']} | {src['label']} | {src['url'][:70]}: "
                  f"{len(items)} gefunden, {len(to_post)} neu")

            for item in to_post:
                if dry_run:
                    extra = item["summary"].replace("\n", " | ") if item.get("is_result") else item["url"]
                    print(f"   → [{label_for(src, item)}] {item['title']}  ({extra})")
                    continue
                try:
                    post_to_discord(webhook, game, src, item)
                except Exception as exc:
                    errors += 1
                    print(f"❌ Posten fehlgeschlagen: {exc}")
                    break
                if not item.get("is_result"):
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
