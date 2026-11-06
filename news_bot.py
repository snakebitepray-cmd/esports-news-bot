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
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
SEEN_LIMIT = 500


# ---------- Hilfsfunktionen ----------

def clean_text(raw: str, limit: int = 300) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.;:!?])(?=\s|$)", r"\1", text)  # "JioTV ," -> "JioTV,"
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


_lol_events_cache: dict = {}


def _norm(x: str) -> str:
    return re.sub(r"[\s_\-]+", " ", (x or "").lower()).strip()


def league_tier(slug: str, name: str, src: dict) -> str | None:
    """'major' (volle Darstellung), 'minor' (kompakt) oder None (ignorieren)."""
    s, n = _norm(slug), _norm(name)
    if any(x in s or x in n for x in map(_norm, src.get("leagues_exclude", []))):
        return None
    if any(k in (s, n) for k in map(_norm, src.get("leagues", []))):
        return "major"
    if any(s.startswith(k) or n.startswith(k) for k in map(_norm, src.get("leagues_minor", []))):
        return "minor"
    return None


_lol_tiers: dict = {}


def lol_events(src: dict) -> list[dict]:
    """Spielplan der gewählten Ligen von lolesports.com (einmal pro Lauf abgerufen)."""
    key = tuple(src["leagues"] + src.get("leagues_minor", []))
    if key in _lol_events_cache:
        return _lol_events_cache[key]
    api = "https://esports-api.lolesports.com/persisted/gw"
    hdr = {**HEADERS, "x-api-key": LOLESPORTS_KEY}
    leagues = requests.get(f"{api}/getLeagues", params={"hl": "en-US"}, headers=hdr, timeout=30)
    leagues.raise_for_status()
    all_leagues = leagues.json()["data"]["leagues"]
    wanted = []
    for lg in all_leagues:
        tier = league_tier(lg.get("slug", ""), lg.get("name", ""), src)
        if tier:
            _lol_tiers[lg.get("slug", "")] = tier
            _lol_tiers[lg.get("name", "")] = tier
            wanted.append(lg)
    if src.get("_debug"):
        print("   Ligen bei lolesports: " + ", ".join(
            f"{lg.get('slug')}{'*' if lg in wanted else ''}" for lg in all_leagues) + "   (* = dabei)")
    if not wanted:
        raise ValueError("Keine der gewünschten Ligen gefunden")
    sched = requests.get(f"{api}/getSchedule", headers=hdr, timeout=30,
                         params={"hl": "en-US", "leagueId": ",".join(lg["id"] for lg in wanted)})
    sched.raise_for_status()
    _lol_events_cache[key] = sched.json()["data"]["schedule"]["events"]
    return _lol_events_cache[key]


def lol_is_minor(ev: dict) -> bool:
    lg = ev.get("league") or {}
    return "minor" in (_lol_tiers.get(lg.get("slug", "")), _lol_tiers.get(lg.get("name", "")))


def fetch_lolesports(src: dict) -> list[dict]:
    """Abgeschlossene Matches der gewählten Ligen von lolesports.com."""
    items = []
    for ev in lol_events(src):
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
            "minor": lol_is_minor(ev),
            "short": f"{league} · " + score_line(t1.get("code") or t1["name"], w1, t2.get("code") or t2["name"], w2),
        })
    return items


def parse_ago(text: str) -> datetime | None:
    """'2h 30m ago' -> Zeitpunkt."""
    units = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
    parts = re.findall(r"(\d+)\s*([wdhms])", text or "")
    if not parts:
        return None
    return datetime.now(timezone.utc) - timedelta(seconds=sum(int(n) * units[u] for n, u in parts))


def vlr_tier(event: str, src: dict) -> str | None:
    """'major', 'minor' (kompakt) oder None. Qualifier zählen immer als 'minor' –
    aber nur, wenn sie zur VCT gehören (nicht z. B. Challengers-Qualifier)."""
    e = event.lower()
    is_vct = any(k in e for k in src.get("vct_words", ["vct", "champions tour"]))
    if any(k in e for k in src.get("events_minor", [])) and is_vct:
        return "minor"
    if any(k in e for k in src.get("events_minor_any", [])):  # z. B. Game Changers (ohne "VCT" im Namen)
        return "minor"
    if any(k in e for k in src.get("events", [])):
        return "major"
    return None


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
        tier = vlr_tier(event, src)
        if not tier:
            continue
        teams = a.select(".match-item-vs-team")
        if len(teams) != 2:
            continue
        names = [t.select_one(".match-item-vs-team-name").get_text(" ", strip=True) for t in teams]
        scores = [t.select_one(".match-item-vs-team-score").get_text(strip=True) for t in teams]
        eta = a.select_one(".ml-eta")
        winner = None
        if scores[0].isdigit() and scores[1].isdigit() and scores[0] != scores[1]:
            winner = names[0] if int(scores[0]) > int(scores[1]) else names[1]
        items.append({
            "winner": winner,
            "id": "vlr-" + a["href"].split("/")[1],
            "title": f"{names[0]} vs {names[1]}",
            "url": urljoin(src["url"], a["href"]),
            "summary": score_line(names[0], scores[0], names[1], scores[1])
                       + "\n" + " · ".join(x for x in (event, series) if x),
            "image": None,
            "date": parse_ago(eta.get_text(" ", strip=True)) if eta else None,
            "source": "vlr.gg",
            "is_result": True,
            "minor": tier == "minor",
            "short": f"{event} · " + score_line(names[0], scores[0], names[1], scores[1]),
        })
    return items


FETCHERS = {
    "rss": lambda src: fetch_rss(src["url"]),
    "riot": lambda src: fetch_riot(src["url"]),
    "lolesports": fetch_lolesports,
    "vlr": fetch_vlr,
}


# ---------- Discord ----------

BOILERPLATE = ("cookie", "subscribe", "newsletter", "sign up", "advertisement", "all rights reserved",
               "follow us", "read more", "click here", "affiliate", "we may earn", "image credit",
               "javascript", "log in", "privacy policy")


def article_lead(soup, limit: int) -> str:
    """Die ersten inhaltlichen Absätze eines Artikels, auf ganze Sätze gekürzt."""
    body = soup.find("article") or soup.find("main") or soup.body or soup
    for junk in body.select("script, style, nav, aside, figure, figcaption, header, footer, form, button"):
        junk.decompose()
    paras = []
    for p in body.find_all(["p", "li"]):
        text = re.sub(r"\s+", " ", p.get_text()).strip()  # ohne Trenner, sonst "2 026" / "O XY"
        if len(text) < 60 or any(b in text.lower() for b in BOILERPLATE) or text in paras:
            continue
        paras.append(text)
        if sum(len(x) for x in paras) >= limit:
            break
    text = " ".join(paras)
    if len(text) > limit:  # an Satzende abschneiden
        cut = text[:limit]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(".\""))
        text = cut[: end + 1] if end > limit * 0.4 else cut.rsplit(" ", 1)[0] + "…"
    return text


def resolve_google(url: str) -> str | None:
    """Wandelt einen Google-News-Link in den echten Artikel-Link um."""
    try:
        from googlenewsdecoder import gnewsdecoder
        res = gnewsdecoder(url, timeout=15)
        if res.get("success", res.get("status")) and res.get("decoded_url"):
            return res["decoded_url"]
        print(f"   (Google-Link nicht auflösbar: {res.get('message')})")
    except Exception as exc:
        print(f"   (Google-Link nicht auflösbar: {exc})")
    return None


def read_article(url: str) -> dict:
    """Artikel laden: Text, Kurzbeschreibung, Bild (leer, wenn die Seite nicht lesbar ist)."""
    from bs4 import BeautifulSoup
    empty = {"teaser": "", "image": None, "text": ""}
    try:
        resp = requests.get(url, headers={**HEADERS, "User-Agent": BROWSER_UA}, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        print(f"   (Artikel nicht lesbar: {exc})")
        return empty
    soup = BeautifulSoup(resp.text, "html.parser")

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content", "").strip():
                return tag["content"].strip()
        return None

    image = meta("og:image", "twitter:image")
    return {
        "teaser": meta("og:description", "twitter:description", "description") or "",
        "image": image if image and image.startswith("http") else None,
        "text": article_lead(soup, 6000),
    }


_last_ai_call = 0.0


def gemini(prompt: str) -> str | None:
    """Fragt Google Gemini (nur wenn GEMINI_API_KEY gesetzt ist). Probiert mehrere Gratis-Modelle."""
    global _last_ai_call
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    for model in config.GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            wait = _last_ai_call + config.GEMINI_PAUSE_SECONDS - time.time()
            if wait > 0:  # Gratis-Stufe erlaubt nur wenige Anfragen pro Minute
                time.sleep(wait)
            _last_ai_call = time.time()
            try:
                r = requests.post(url, json=body, headers={"x-goog-api-key": key}, timeout=60)
            except Exception as exc:
                print(f"   (KI {model}: {exc})")
                break
            if r.ok:
                try:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                except (KeyError, IndexError, ValueError):
                    print(f"   (KI {model}: leere Antwort)")
                    break
                out = " ".join(p.get("text", "") for p in parts).strip()
                return re.sub(r"\s*\[\d+(,\s*\d+)*\]", "", out).strip()  # Quellen-Nummern entfernen
            try:
                msg = r.json().get("error", {}).get("message", "")
            except ValueError:
                msg = r.text
            msg = re.sub(r"\s+", " ", msg)[:200]
            print(f"   (KI {model}: {r.status_code} {msg})")
            # Überlastet / zu viele Anfragen -> 30 s warten und nochmal; Modell fehlt -> nächstes Modell
            if attempt == 0 and (r.status_code in (500, 503) or (r.status_code == 429 and "limit: 0" not in msg)):
                time.sleep(30)
                continue
            if r.status_code == 429 and "limit: 0" not in msg:
                return None  # Kontingent gerade erschöpft
            break
    return None


def ai_summary(title: str, text: str, source: str, game_name: str = "") -> dict:
    """Kurze Zusammenfassung per KI. Gibt ein dict zurück:
    summary (Text), subjects ([Spieler, Team, Liga] für die Bildsuche), relevant (passt zum Spiel?),
    category ("TRANSFER" oder "ESPORTS")."""
    result = {"summary": None, "subjects": [], "relevant": None, "category": None}
    if len(text) < 200 or not os.getenv("GEMINI_API_KEY"):
        return result  # ohne Artikeltext keine Zusammenfassung (sonst würde die KI raten)
    rules = ""
    if game_name:
        rules = (f"First line exactly 'RELEVANT: YES' if the article is about {game_name} (the video game or "
                 f"its esports scene), otherwise exactly 'RELEVANT: NO'.\n"
                 f"Second line exactly 'CATEGORY: TRANSFER' if the article is mainly about a player or coach "
                 f"joining, leaving, signing, re-signing, being benched, released or retiring, or a team's roster "
                 f"changes; otherwise exactly 'CATEGORY: ESPORTS'.\nThen: ")
    rules += ("Write a concise summary for a Discord esports news channel: 2-3 short sentences in English, "
             "only facts, include the key names/teams/numbers, refer to players and coaches only by their "
             "in-game names (e.g. 'Zellsis', not their real names), no intro phrases like 'This article', "
             "no markdown, no hashtags.")
    rules += ("\nAfter the summary, add one last line exactly like 'IMAGE: <player> | <team> | <league>' with "
              "the in-game name of the most relevant player or coach, the most relevant team and the league or "
              "tournament (use NONE for anything not mentioned).")
    out = gemini(f"{rules}\n\nTitle: {title}\n\nArticle:\n{text[:6000]}")
    if not out:
        return result
    m = re.match(r"\s*\**RELEVANT:\s*(YES|NO)\**\s*", out, re.I)
    if m:
        result["relevant"] = m.group(1).upper() == "YES"
        out = out[m.end():].strip()
    m = re.match(r"\s*\**CATEGORY:\s*(TRANSFER|ESPORTS)\**\s*", out, re.I)
    if m:
        result["category"] = m.group(1).upper()
        out = out[m.end():].strip()
    subjects = []
    m = re.search(r"\s*IMAGE:\s*(.+?)\s*$", out, re.I)
    if m:
        out = out[: m.start()].strip()
        for name in m.group(1).split("|"):
            name = name.strip(" *.'\"<>")
            if name and name.upper() != "NONE":
                subjects.append(name)
    result["subjects"] = subjects
    if out and not out.upper().startswith("NONE"):
        result["summary"] = out[:900]
    return result


def ai_same_story(a: str, b: str) -> bool:
    """Grenzfall: entscheidet per KI, ob zwei Überschriften dieselbe Meldung sind."""
    out = gemini("Do these two esports news headlines report the same news event (same people/teams and the "
                 "same thing happening)? Answer only YES or NO.\n\n1. " + a + "\n2. " + b)
    return bool(out) and out.strip().upper().startswith("YES")


STOPWORDS = {"sources", "source", "report", "reports", "the", "a", "an", "to", "for", "of", "in", "on", "and",
             "as", "with", "at", "set", "after", "from", "by", "is", "will", "their", "his", "her"}


def keywords(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in STOPWORDS and len(w) > 1}


# Wörter, die bei fast jeder Meldung vorkommen und für den Duplikat-Vergleich nichts aussagen
DEDUP_STOP = STOPWORDS | {
    "2024", "2025", "2026", "2027", "2028", "esports", "season", "league", "legends", "lol", "valorant",
    "vct", "team", "new", "year", "split", "official", "officially", "announces", "announced", "announce",
    "sign", "signs", "signed", "signing", "join", "joins", "joined", "joining", "reportedly", "rumor",
    "rumors", "according", "confirms", "confirmed", "after", "ahead", "update", "news", "exclusive",
    "worlds", "msi", "masters", "champions", "stage", "week", "playoffs", "finals", "final", "semifinals",
    "semifinal", "quarterfinals", "quarterfinal", "kickoff", "first", "stand", "match", "matches", "game",
}


def dedup_keywords(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in DEDUP_STOP and len(w) > 1}


def is_duplicate(title: str, recent: list[dict]) -> str | None:
    """Gibt den Titel einer schon geposteten Meldung zurück, wenn es dieselbe Story ist."""
    kw = dedup_keywords(title)
    if not kw:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.DEDUP_HOURS)
    borderline = []
    for r in recent:
        ts = parse_date(r.get("ts"))
        if ts and ts < cutoff:
            continue
        if norm_title(r["t"]) == norm_title(title):
            return r["t"]
        rk = dedup_keywords(r["t"])
        common = kw & rk
        smaller = min(len(kw), len(rk))
        if smaller and len(common) >= min(3, smaller) and len(common) / smaller >= config.DEDUP_SIMILARITY:
            return r["t"]
        borderline.append((len(common), r["t"]))
    # Grenzfälle (2 gleiche Namen, z. B. "G2" + "Perkz"): die KI fragen, höchstens 2 Mal pro Artikel
    for n, other in sorted(borderline, reverse=True)[:2]:
        if n >= 2 and ai_same_story(title, other):
            return other
    return None


def find_other_source(item: dict) -> dict | None:
    """Sucht über Google News einen anderen, lesbaren Artikel zur selben Meldung."""
    from config import google_news
    title = re.sub(r"^(sources?|report|reports|breaking)\s*:\s*", "", item["title"], flags=re.I)
    want = keywords(title)
    try:
        entries = fetch_rss(google_news(title))
    except Exception as exc:
        print(f"   (andere Quelle: Suche fehlgeschlagen: {exc})")
        return None
    tried = 0
    for e in entries:
        if tried >= 3:
            break
        if e["source"] and e["source"].lower() == (item.get("source") or "").lower():
            continue
        overlap = len(want & keywords(e["title"])) / max(len(want), 1)
        if overlap < 0.6:  # muss klar dieselbe Meldung sein
            continue
        tried += 1
        url = resolve_google(e["url"]) if "news.google.com" in e["url"] else e["url"]
        if not url:
            continue
        page = read_article(url)
        if len(page["text"]) >= 200:
            print(f"   (Text von anderer Quelle: {e['source']})")
            page["url"] = url
            return page
    print("   (keine lesbare andere Quelle gefunden)")
    return None


PLAYER_PATTERNS = [
    r"\bparts? ways with\s+([\w.\-]+)",
    r"^(?:\w+\s*:\s*)?([\w.\-]+)\s+(?:is\s+)?(?:set to join|to join|joins|has joined|signs with|re-signs with|"
    r"extends|moves to|leaves|departs|retires)",
    r"\b(?:sign|signs|signed|re-signs|acquire|acquires|pick|picks|welcome|welcomes|promote|promotes|bench|benches|"
    r"release|releases)\s+([\w.\-]+)",
]


def team_from_title(title: str) -> str | None:
    """Notlösung ohne KI: neues Team aus Titeln wie '... join Bilibili Gaming for 2027' lesen."""
    m = re.search(r"\b(?:join|joins|joined|to|with)\s+((?:[A-Z0-9][\w.&'-]*\s?){1,3}?)\s*"
                  r"(?:for|ahead|as|in|after|through|until|on|,|$)", title)
    if m:
        team = m.group(1).strip()
        if team.upper() not in {"LFL", "VCT", "LEC", "LCK", "LPL", "LCS", "THE"} and not team.isdigit():
            return team
    return None


def player_from_title(title: str) -> str | None:
    """Notlösung ohne KI: Spielernamen aus typischen Transfer-Titeln lesen."""
    for pat in PLAYER_PATTERNS:
        m = re.search(pat, title, re.I)
        if m and m.group(1).lower() not in {"the", "a", "an", "with", "their", "new", "former", "star"}:
            return m.group(1)
    return None


_last_wiki_call = 0.0
_liquipedia_blocked = False
_lol_people: dict | None = None
_riot_fallback: dict = {}
BOT_UA = f"EsportsNewsDiscordBot/1.0 ({config.CONTACT})"


def liquipedia_image(name: str, wiki: str) -> str | None:
    """Bild von Liquipedia (offizielle API, langsam; wird nach einer Sperre nicht mehr gefragt)."""
    global _last_wiki_call, _liquipedia_blocked
    if _liquipedia_blocked:
        return None
    wait = _last_wiki_call + 2.5 - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_wiki_call = time.time()
    try:
        r = requests.get(f"https://liquipedia.net/{wiki}/api.php", timeout=20,
                         headers={"User-Agent": BOT_UA, "Accept-Encoding": "gzip"},
                         params={"action": "query", "titles": name, "redirects": 1, "prop": "pageimages",
                                 "piprop": "thumbnail", "pithumbsize": 600, "format": "json"})
        if r.status_code in (403, 429):
            _liquipedia_blocked = True
            print(f"   (Liquipedia blockt gerade ({r.status_code}) – wird in diesem Lauf nicht mehr gefragt)")
            return None
        r.raise_for_status()
        for page in r.json().get("query", {}).get("pages", {}).values():
            src = (page.get("thumbnail") or {}).get("source")
            if src:
                return src
    except Exception as exc:
        print(f"   (Liquipedia: {exc})")
    return None


def lolesports_image(name: str) -> str | None:
    """Offizielles Spielerfoto oder Team-Logo aus den lolesports-Daten (nur LoL)."""
    global _lol_people
    if _lol_people is None:
        _lol_people = {}
        try:
            r = requests.get("https://esports-api.lolesports.com/persisted/gw/getTeams", timeout=30,
                             params={"hl": "en-US"}, headers={**HEADERS, "x-api-key": LOLESPORTS_KEY})
            r.raise_for_status()
            for t in r.json()["data"]["teams"]:
                for key in (t.get("name"), t.get("code"), t.get("slug")):
                    if key and t.get("image"):
                        _lol_people.setdefault(("team", key.lower()), t["image"])
                for pl in t.get("players") or []:
                    img = pl.get("image") or ""
                    if pl.get("summonerName") and img and "default" not in img.lower():
                        _lol_people.setdefault(("player", pl["summonerName"].lower()), img)
        except Exception as exc:
            print(f"   (lolesports-Bilder nicht abrufbar: {exc})")
    n = name.lower()
    return _lol_people.get(("player", n)) or _lol_people.get(("team", n))


ESPORTS_HINTS = ("esport", "league of legends", "valorant", "gamer", "video game", "professional player",
                 "gaming", "tournament", "championship", "league")


def wikipedia_image(name: str) -> str | None:
    """Bild von Wikipedia – nur wenn die Seite wirklich zu Esports gehört."""
    for title in (name, f"{name} (gamer)", f"{name} (esports)"):
        try:
            r = requests.get("https://en.wikipedia.org/w/api.php", timeout=20, headers={"User-Agent": BOT_UA},
                             params={"action": "query", "titles": title, "redirects": 1, "format": "json",
                                     "prop": "pageimages|description", "piprop": "thumbnail",
                                     "pithumbsize": 600, "pilicense": "any"})
            r.raise_for_status()
            for page in r.json().get("query", {}).get("pages", {}).values():
                desc = (page.get("description") or "").lower()
                src = (page.get("thumbnail") or {}).get("source")
                if src and any(h in desc for h in ESPORTS_HINTS):
                    return src
        except Exception as exc:
            print(f"   (Wikipedia: {exc})")
            return None
    return None


def riot_fallback_image(game: dict) -> str | None:
    """Letzter Ausweg: Bild des neuesten offiziellen Riot-Artikels zum Spiel."""
    key = game["name"]
    if key not in _riot_fallback:
        _riot_fallback[key] = None
        for src in game["sources"]:
            if src["type"] != "riot":
                continue
            try:
                items = [i for i in fetch_riot(src["url"]) if i.get("image")]
                items.sort(key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc))
                if items:
                    _riot_fallback[key] = items[-1]["image"]
                    break
            except Exception as exc:
                print(f"   (Riot-Ersatzbild nicht abrufbar: {exc})")
    return _riot_fallback[key]


def find_image(names: list[str], game: dict) -> tuple[str | None, str]:
    """Sucht ein passendes Bild zu Spieler/Team/Liga. Gibt (Bild, Herkunft) zurück."""
    for name in names:
        if game.get("liquipedia") == "leagueoflegends":
            img = lolesports_image(name)
            if img:
                return img, f"{name} (lolesports)"
        img = wikipedia_image(name)
        if img:
            return img, f"{name} (Wikipedia)"
        if game.get("liquipedia"):
            img = liquipedia_image(name, game["liquipedia"])
            if img:
                return img, f"{name} (Liquipedia)"
    if game.get("default_image"):
        return game["default_image"], "eigenes Ersatzbild"
    img = riot_fallback_image(game)
    return img, "neuestes Riot-Bild" if img else "nichts gefunden"


def is_relevant(item: dict, text: str, game: dict) -> bool:
    """Ohne KI: Passt der Artikel zum Spiel? (Stichwörter im Titel/Text oder bekannte Esports-Seite)"""
    haystack = f"{item['title']} {text}".lower()
    if any(k in haystack for k in game.get("relevance", [])):
        return True
    source = (item.get("source") or "").lower()
    return any(s in source for s in config.ESPORTS_SOURCES) and not text


def enrich(item: dict, game: dict | None = None, label: str = "", check: bool = False) -> None:
    """Holt Zusammenfassung, Bild und direkten Link zum Artikel.
    check=True: prüft zusätzlich, ob der Artikel überhaupt zum Spiel passt (setzt item['irrelevant'])."""
    url = item["url"]
    if "news.google.com" in url:
        url = resolve_google(url)
        if url:
            item["url"] = url
    page = read_article(url) if url else {"teaser": "", "image": None, "text": ""}
    image_src = url  # Seite, von der das Bild stammt
    if len(page["text"]) < 200:  # Seite gesperrt oder kaum Text -> andere Seite zur selben Meldung
        other = find_other_source(item)
        if other:
            if not page["image"]:
                image_src = other["url"]
            page = {"teaser": page["teaser"] or other["teaser"], "text": other["text"],
                    "image": page["image"] or other["image"]}

    # 1) Bild der News-Seite (nur von bekannten Esports-Seiten, sonst oft zufällige Stockfotos)
    site = urlparse(image_src or "").netloc.lower().removeprefix("www.")
    trusted = any(site == d or site.endswith("." + d) for d in config.TRUSTED_IMAGE_SITES)
    if not item.get("image") and page["image"] and trusted:
        item["image"] = page["image"]

    ai = ai_summary(item["title"], page["text"], item.get("source", ""), game["name"] if (check and game) else "")
    summary, subjects, ai_relevant = ai["summary"], ai["subjects"], ai["relevant"]
    used_ai = bool(summary)
    if check and ai["category"]:
        item["category"] = ai["category"]  # Kategorie nach Inhalt statt nach Titel
    if check and game:
        relevant = ai_relevant if ai_relevant is not None else is_relevant(item, page["text"], game)
        if not relevant:
            item["irrelevant"] = True
            print(f"   🚫 Passt nicht zu {game['name']} ({'KI' if ai_relevant is not None else 'Stichwörter'})")
            return

    # 2) Kein Bild -> passendes Bild suchen: Spieler, Team, Liga (lolesports, Wikipedia, Liquipedia),
    #    zuletzt eigenes Ersatzbild bzw. neuestes offizielles Riot-Bild
    if not item.get("image") and config.FALLBACK_IMAGES and game and game.get("liquipedia"):
        guess = player_from_title(item["title"])
        acronyms = [w for w in re.findall(r"\b[A-Z]{2,5}\b", item["title"]) if w not in {"VOD", "NA", "EU", "AI"}]
        team = team_from_title(item["title"])
        candidates = subjects + [x for x in (guess, team) if x] + acronyms  # z. B. "LCK", "VCT"
        img, origin = find_image(list(dict.fromkeys(candidates)), game)  # doppelte raus
        print(f"   🖼️ Bild gesucht: {origin}")
        item["image"] = img

    if not summary:  # ohne KI: Artikelanfang bzw. Kurzbeschreibung
        lead = page["text"][: config.SUMMARY_LENGTH + 200]
        summary = lead if len(lead) >= 120 else (page["teaser"] or lead)
    if summary and norm_title(summary) != norm_title(item["title"]):
        item["summary"] = clean_text(summary, 900 if used_ai else config.SUMMARY_LENGTH + 50)
    if not item["summary"]:  # Artikel nicht lesbar und keine andere Quelle -> kurzer Hinweis statt leer
        source = item.get("source") or urlparse(item["url"] or "").netloc.removeprefix("www.")
        kind = "Exclusive report" if re.match(r"^(sources?|report|exclusive)\s*:", item["title"], re.I) else "Report"
        item["summary"] = (f"*{kind} by {source} – full details in the article.*" if source
                           else "*Full details in the article.*")
    print(f"   📝 Zusammenfassung: {'KI' if used_ai else ('Text' if item['summary'] else 'keine')}"
          f" | Bild: {'ja' if item.get('image') else 'nein'}")


def has_word(title: str, words) -> bool:
    """True, wenn eines der Wörter (als Wortanfang, z. B. 'sign' -> 'signs') im Titel steht."""
    return any(re.search(r"\b" + re.escape(w), title, re.I) for w in words)


def label_for(src: dict, item: dict) -> str:
    """Kategorie eines News-Artikels: nach KI-Einschätzung, sonst nach Wörtern im Titel."""
    if src["type"] != "rss":
        return src["label"]  # Patch-Notes, Riot-Esports, Ergebnisse: feste Kategorie
    if item.get("category") == "TRANSFER":
        return config.TRANSFER_LABEL
    if item.get("category") == "ESPORTS":
        return config.ESPORTS_LABEL
    title = item["title"]
    if has_word(title, config.TRANSFER_LABEL_WORDS) and not has_word(title, config.NOT_TRANSFER_WORDS):
        return config.TRANSFER_LABEL
    return config.ESPORTS_LABEL if src["label"] == config.TRANSFER_LABEL else src["label"]


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

    send_embed(webhook, embed)


def send_embed(webhook: str, embed: dict, wait: bool = False) -> dict | None:
    """Postet ein Embed. Mit wait=True gibt Discord die Nachricht zurück (für die ID zum späteren Löschen)."""
    for _ in range(5):
        r = requests.post(webhook, json={"embeds": [embed]}, timeout=30, params={"wait": "true"} if wait else None)
        if r.status_code == 429:  # Rate-Limit: kurz warten und erneut versuchen
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        time.sleep(1)
        return r.json() if wait else None
    raise RuntimeError("Discord Rate-Limit dauerhaft überschritten")


def delete_message(webhook: str, message_id: str) -> None:
    """Löscht eine eigene Nachricht des Webhooks (z. B. den vorherigen Spielplan)."""
    try:
        r = requests.delete(f"{webhook.split('?')[0]}/messages/{message_id}", timeout=30)
        if r.status_code not in (204, 404):  # 404 = schon gelöscht
            print(f"   (alter Spielplan nicht löschbar: {r.status_code})")
    except Exception as exc:
        print(f"   (alter Spielplan nicht löschbar: {exc})")


# ---------- Spielplan: Tagesübersicht & Erinnerungen ----------

def upcoming_lol(src: dict) -> list[dict]:
    out = []
    for ev in lol_events(src):
        match = ev.get("match") or {}
        teams = match.get("teams") or []
        if ev.get("state") not in ("unstarted", "inProgress") or len(teams) != 2:
            continue
        start = parse_date(ev.get("startTime"))
        if not start:
            continue
        bo = (match.get("strategy") or {}).get("count")
        slug = (ev.get("league") or {}).get("slug", "")
        out.append({
            "id": f'lol-{match.get("id")}', "start": start,
            "t1": teams[0].get("name") or "TBD", "t2": teams[1].get("name") or "TBD",
            "event": (ev.get("league") or {}).get("name", ""), "stage": ev.get("blockName") or "",
            "bestof": f"Bo{bo}" if bo else "", "url": None,
            "live": ev.get("state") == "inProgress",  # lolesports meldet selbst, wenn ein Match läuft
            "minor": lol_is_minor(ev),
            "watch": f"https://lolesports.com/live/{slug}" if slug else "https://lolesports.com",
        })
    return out


def parse_in(text: str) -> datetime | None:
    """'2h 30m' (bis Matchbeginn) -> Zeitpunkt, auf 5 Minuten gerundet (18:59 -> 19:00)."""
    units = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
    parts = re.findall(r"(\d+)\s*([wdhms])", text or "")
    if not parts:
        return None
    ts = datetime.now(timezone.utc).timestamp() + sum(int(n) * units[u] for n, u in parts)
    return datetime.fromtimestamp(round(ts / 300) * 300, timezone.utc)


def upcoming_vlr(src: dict) -> list[dict]:
    from bs4 import BeautifulSoup
    resp = requests.get(src["url"].replace("/matches/results", "/matches"), headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for a in soup.select("a.match-item"):
        ev_el = a.select_one(".match-item-event")
        series_el = a.select_one(".match-item-event-series")
        stage = series_el.get_text(" ", strip=True) if series_el else ""
        if series_el:
            series_el.extract()
        event = ev_el.get_text(" ", strip=True) if ev_el else ""
        tier = vlr_tier(event, src)
        if not tier:
            continue
        names = [t.get_text(" ", strip=True) for t in a.select(".match-item-vs-team-name")]
        eta = a.select_one(".ml-eta")
        eta_text = eta.get_text(" ", strip=True) if eta else ""
        if "ago" in eta_text.lower():  # schon gespielt
            continue
        status = a.select_one(".ml-status")
        live = bool(status and "live" in status.get_text(" ", strip=True).lower()) or bool(a.select_one(".mod-live"))
        start = datetime.now(timezone.utc) if live and not eta_text else parse_in(eta_text)
        if len(names) != 2 or not start:
            continue
        url = urljoin(src["url"], a["href"])
        out.append({
            "id": "vlr-" + a["href"].split("/")[1], "start": start, "t1": names[0] or "TBD",
            "t2": names[1] or "TBD", "event": event, "stage": stage, "bestof": "",
            "url": url, "live": live, "watch": url, "minor": tier == "minor",
        })
    return out


UPCOMING = {"lolesports": upcoming_lol, "vlr": upcoming_vlr}


def local_now() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(config.TIMEZONE))


def match_line(m: dict) -> str:
    ts = int(m["start"].timestamp())
    teams = f"**{m['t1']}** vs **{m['t2']}**"
    if m["url"]:
        teams = f"[{m['t1']} vs {m['t2']}]({m['url']})"
    extra = " · ".join(x for x in (m["stage"], m["bestof"]) if x)
    return f"<t:{ts}:t> {teams}" + (f" ({extra})" if extra else "")


def handle_schedule(game: dict, game_state: dict, webhook: str | None, dry_run: bool) -> list[dict]:
    """Stündlicher Spielplan und Benachrichtigung, wenn ein Match live geht."""
    matches = []
    for src in game["sources"]:
        if src["type"] in UPCOMING:
            try:
                matches += UPCOMING[src["type"]](src)
            except Exception as exc:
                print(f"❌ {game['name']} | Spielplan: {exc}")
    now = datetime.now(timezone.utc)
    matches.sort(key=lambda m: m["start"])
    print(f"📅 {game['name']} | Spielplan: {len(matches)} kommende Matches gefunden "
          f"({sum(1 for m in matches if m.get('live'))} live)")

    # 1) Spielplan: jede Stunde neu (Matches der nächsten 24 Stunden), der vorherige Post wird gelöscht
    local = local_now()
    hour_key = local.strftime("%Y-%m-%d %H")
    if config.SCHEDULE_EVERY_HOUR and (game_state.get("schedule_hour") != hour_key or dry_run):
        live_now = [m for m in matches if m.get("live")]
        window = [m for m in matches if not m.get("live")
                  and now - timedelta(minutes=15) <= m["start"] <= now + timedelta(hours=config.SCHEDULE_WINDOW_HOURS)]
        fields = []
        if live_now:
            fields.append({"name": "🔴 Live now", "inline": False, "value": "\n".join(
                f"{m['event']}: **{m['t1']}** vs **{m['t2']}**" for m in live_now)[:1024]})
        groups: dict = {}
        for m in window:
            if not m.get("minor"):
                groups.setdefault(m["event"] or game["name"], []).append(match_line(m))
        fields += [{"name": ev[:256], "value": "\n".join(lines)[:1024], "inline": False}
                   for ev, lines in list(groups.items())[:22]]
        minor = [f"<t:{int(m['start'].timestamp())}:t> {m['event']}: {m['t1']} vs {m['t2']}"
                 for m in window if m.get("minor")]
        if minor:  # kleinere Ligen kompakt in einem Abschnitt
            fields.append({"name": "Other leagues", "value": "\n".join(minor)[:1024], "inline": False})

        old_id = game_state.get("schedule_msg")
        if dry_run:
            print(f"   → Spielplan (Vorschau, stündlich): {len(fields)} Abschnitte")
            for f in fields:
                print(f"     {f['name']}: " + " | ".join(f["value"].splitlines()))
        elif webhook:
            if old_id and config.SCHEDULE_REPLACE_PREVIOUS:
                delete_message(webhook, old_id)  # nur der neueste Spielplan bleibt im Kanal
                game_state["schedule_msg"] = None
            if fields:
                embed = {"title": f"📅 Upcoming matches – next {config.SCHEDULE_WINDOW_HOURS} hours",
                         "color": game["color"], "author": {"name": f"🗓️ Schedule · {game['name']}"},
                         "fields": fields, "footer": {"text": "Times shown in your local time · updated"},
                         "timestamp": now.isoformat()}
                msg = send_embed(webhook, embed, wait=True)
                game_state["schedule_msg"] = (msg or {}).get("id")
            else:
                print("   (keine Matches in den nächsten 24 Stunden – kein Spielplan-Post)")
            game_state["schedule_hour"] = hour_key

    # 2) Benachrichtigung, sobald ein Match live ist
    if config.LIVE_NOTIFICATIONS:
        notified = game_state.setdefault("live_notified", [])
        live = [m for m in matches if m.get("live") and m["id"] not in notified and "TBD" not in (m["t1"], m["t2"])
                and (not m.get("minor") or config.LIVE_NOTIFICATIONS_MINOR)]
        for m in live:
            embed = {"title": f"🔴 LIVE: {m['t1']} vs {m['t2']}"[:256], "color": game["color"],
                     "author": {"name": f"🔴 Match started · {game['name']}"},
                     "description": " · ".join(x for x in (m["event"], m["stage"], m["bestof"]) if x)
                                    + (f"\n[Watch live]({m['watch']})" if m.get("watch") else "")}
            if m.get("watch"):
                embed["url"] = m["watch"]
            if dry_run:
                print(f"   → LIVE-Benachrichtigung: {m['t1']} vs {m['t2']}")
            elif webhook:
                send_embed(webhook, embed)
                notified.append(m["id"])
        game_state["live_notified"] = notified[-300:]
    return matches


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
        recent = game_state.setdefault("recent", [])  # zuletzt gepostete Titel (für Duplikat-Prüfung)

        for src in game["sources"]:
            seen = game_state.setdefault(src["url"], [])
            first_run = not seen
            if dry_run and src["type"] == "lolesports":
                src = {**src, "_debug": True}  # im Test-Modus alle verfügbaren Ligen anzeigen
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
                and (i.get("is_result") or len(i["title"].split()) >= config.MIN_TITLE_WORDS)
                and not any(x in (i.get("source") or "").lower() for x in config.EXCLUDE_SOURCES)
                and (not src.get("title_must_contain") or has_word(i["title"], src["title_must_contain"]))
            ]
            fresh.sort(key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc))
            # Beim allerersten Lauf nur den neuesten Artikel posten (kein Spam)
            to_post = fresh[-1:] if first_run else fresh[-config.MAX_POSTS_PER_SOURCE:]
            print(f"✅ {game['name']} | {src['label']} | {src['url'][:70]}: "
                  f"{len(items)} gefunden, {len(to_post)} neu")

            minor_results = [i for i in to_post if i.get("is_result") and i.get("minor")]
            to_post = [i for i in to_post if not (i.get("is_result") and i.get("minor"))]
            if minor_results:  # kleinere Ligen: alle Ergebnisse gesammelt in einem kompakten Post
                lines = [i["short"] for i in minor_results][:20]
                embed = {"title": "📊 Other results", "description": "\n".join(lines)[:4000],
                         "color": game["color"], "author": {"name": f"📊 Results · {game['name']}"}}
                if dry_run:
                    print("   → Kompakter Ergebnis-Post: " + " | ".join(lines))
                else:
                    try:
                        send_embed(webhook, embed)
                    except Exception as exc:
                        errors += 1
                        print(f"❌ Posten fehlgeschlagen: {exc}")

            for item in to_post:
                if not item.get("is_result"):
                    dup = is_duplicate(item["title"], recent)
                    if dup:  # dieselbe Story wurde (mit anderem Titel) schon gepostet
                        print(f"   ⏭️ Duplikat übersprungen: {item['title']}\n      (schon gepostet: {dup})")
                        continue
                    enrich(item, game, label_for(src, item), check=src["type"] == "rss")
                    if item.get("irrelevant"):
                        continue
                elif not item.get("thumbnail") and config.FALLBACK_IMAGES and not config.SPOILER_RESULTS:
                    # z. B. Valorant (vlr.gg liefert keine Logos): Logo des Siegers suchen
                    img, origin = find_image([item["winner"]] if item.get("winner") else [], game)
                    print(f"   🖼️ Bild gesucht: {origin}")
                    item["thumbnail"] = img
                if dry_run:
                    extra = item["summary"].replace("\n", " | ") if item.get("is_result") else item["url"]
                    print(f"   → [{label_for(src, item)}] {item['title']}  ({extra})")
                    if item["summary"] and not item.get("is_result"):
                        print(f"     📝 {item['summary']}")
                        recent.append({"t": item["title"], "ts": datetime.now(timezone.utc).isoformat()})
                    continue
                try:
                    target = webhook
                    if src.get("patch") and os.getenv(game.get("patch_webhook_env", "")):
                        target = os.getenv(game["patch_webhook_env"])  # eigener Patch-Kanal
                    post_to_discord(target, game, src, item)
                except Exception as exc:
                    errors += 1
                    print(f"❌ Posten fehlgeschlagen: {exc}")
                    break
                if not item.get("is_result"):
                    seen_titles.add(norm_title(item["title"]))
                    recent.append({"t": item["title"], "ts": datetime.now(timezone.utc).isoformat()})

            # Alles Gefundene als gesehen markieren (auch Übersprungenes)
            ids = [i["id"] for i in items if i["id"]]
            game_state[src["url"]] = (ids + [s for s in seen if s not in ids])[:SEEN_LIMIT]
        game_state["titles"] = list(seen_titles)[-SEEN_LIMIT:]
        game_state["recent"] = recent[-300:]
        handle_schedule(game, game_state, webhook, dry_run)

    if not dry_run:
        STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    return 1 if errors and dry_run else 0


def test_ai() -> int:
    """Testet Zusammenfassung + Bild am neuesten Artikel jeder News-Quelle (postet nichts)."""
    print(f"KI-Schlüssel gesetzt: {'ja' if os.getenv('GEMINI_API_KEY') else 'NEIN'}")
    for game in config.GAMES.values():
        for src in game["sources"]:
            if src["type"] != "rss":
                continue
            try:
                excluded = [k.lower() for k in config.EXCLUDE_KEYWORDS]
                items = [i for i in FETCHERS["rss"](src)
                         if len(i["title"].split()) >= config.MIN_TITLE_WORDS
                         and not any(k in i["title"].lower() for k in excluded)
                         and not any(x in (i.get("source") or "").lower() for x in config.EXCLUDE_SOURCES)
                         and (not src.get("title_must_contain") or has_word(i["title"], src["title_must_contain"]))]
            except Exception as exc:
                print(f"❌ {src['url'][:60]}: {exc}")
                continue
            if not items:
                continue
            item = max(items, key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc))
            print(f"\n🧪 {game['name']} | {label_for(src, item)} | {item['title']}")
            enrich(item, game, label_for(src, item), check=True)
            print(f"   Kategorie: {label_for(src, item)}")
            print(f"   Link: {item['url']}")
            print(f"   Bild: {item.get('image')}")
            print(f"   Text: {item['summary'] or '-'}")
    return 0


if __name__ == "__main__":
    if "--test-ai" in sys.argv:
        sys.exit(test_ai())
    sys.exit(main(dry_run="--dry-run" in sys.argv))
