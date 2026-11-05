"""Hier stellst du ein, welche Quellen in welchen Kanal gepostet werden.

Typen:
  "riot" – offizielle Riot-Newsseite (leagueoflegends.com / playvalorant.com)
  "rss"  – beliebiger RSS/Atom-Feed

"label" erscheint über jeder Nachricht, damit man Patch-Notes und Esports-News
unterscheiden kann. Quellen kannst du einfach hinzufügen oder auskommentieren.
"""

# Sprache der offiziellen Riot-Seiten (z. B. "en-us" oder "de-de")
RIOT_LOCALE = "en-us"


def google_news(query: str) -> str:
    """Google-News-Suche als RSS-Feed (englische Ergebnisse)."""
    from urllib.parse import quote
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"


TRANSFER_LABEL = "🔄 Transfer"

# Bei Transfer-Quellen muss eines dieser Wörter im TITEL stehen.
# Esports-Artikel mit einem dieser Wörter im Titel bekommen ebenfalls das Transfer-Label.
TRANSFER_WORDS = [
    "roster", "sign", "join", "transfer", "parts ways", "part ways", "free agent", "bench",
    "leave", "leaving", "depart", "contract", "acquire", "release", "retire", "promote", "replace",
]

# Ergebnisse als Spoiler verstecken (||3 - 1||)? True = ja
SPOILER_RESULTS = False


GAMES = {
    "lol": {
        "name": "League of Legends",
        "liquipedia": "leagueoflegends",  # Wiki für Bilder (Spieler, Teams, Ligen)
        "default_image": "",  # optional: eigenes Ersatzbild (URL), sonst Spiel-Logo von Liquipedia
        "webhook_env": "LOL_WEBHOOK_URL",  # GitHub-Secret mit der Webhook-URL
        # Optional eigener Kanal für Patch-Notes. Ist das Secret nicht gesetzt,
        # landen Patch-Notes im normalen Kanal.
        "patch_webhook_env": "LOL_PATCH_WEBHOOK_URL",
        "color": 0xC89B3C,
        "sources": [
            {"type": "riot", "label": "🛠️ Update", "patch": True,
             "url": f"https://www.leagueoflegends.com/{RIOT_LOCALE}/news/game-updates/"},
            {"type": "riot", "label": "🏆 Esports",
             "url": f"https://www.leagueoflegends.com/{RIOT_LOCALE}/news/esports/"},
            # Spielerwechsel (steht VOR den allgemeinen News, damit sie das Transfer-Label bekommen)
            {"type": "rss", "label": TRANSFER_LABEL, "title_must_contain": TRANSFER_WORDS,
             "url": google_news('(LEC OR LCK OR "LPL esports" OR LCS OR "League of Legends" OR "LoL Esports") '
                                '(roster OR signs OR joins OR transfer OR "parts ways" OR "free agent" OR benched) when:2d')},
            {"type": "rss", "label": "🏆 Esports",
             "url": google_news('"LoL Esports" OR LEC OR LCK OR "LPL esports" OR "League of Legends Worlds" when:2d')},
            # Match-Ergebnisse von lolesports.com. Ligen per Kürzel (Anfang des "slug").
            {"type": "lolesports", "label": "📊 Result",
             "url": "https://esports-api.lolesports.com/persisted/gw/getSchedule",
             "leagues": ["worlds", "msi", "first_stand", "lec", "lck", "lpl", "lcs", "lta", "lcp", "cblol"]},
        ],
    },
    "valo": {
        "name": "Valorant",
        "liquipedia": "valorant",
        "default_image": "",
        "webhook_env": "VALO_WEBHOOK_URL",
        "patch_webhook_env": "VALO_PATCH_WEBHOOK_URL",
        "color": 0xFF4655,
        "sources": [
            {"type": "riot", "label": "🛠️ Update", "patch": True,
             "url": f"https://playvalorant.com/{RIOT_LOCALE}/news/game-updates/"},
            {"type": "rss", "label": TRANSFER_LABEL, "title_must_contain": TRANSFER_WORDS,
             "url": google_news('(Valorant OR "VCT 2027" OR "VCT EMEA" OR "VCT Americas" OR "VCT Pacific" OR "VCT China") '
                                '(roster OR signs OR joins OR transfer OR "parts ways" OR "free agent" OR benched) when:2d')},
            {"type": "rss", "label": "🏆 Esports",
             "url": google_news('"Valorant Champions Tour" OR "Valorant esports" OR "VCT 2027" OR "VCT EMEA" OR '
                                '"VCT Americas" OR "VCT Pacific" OR "VCT China" OR "VCT Masters" when:2d')},
            # Match-Ergebnisse von vlr.gg, nur Events deren Name eines dieser Wörter enthält
            {"type": "vlr", "label": "📊 Result",
             "url": "https://www.vlr.gg/matches/results",
             "events": ["champions tour", "valorant champions", "valorant masters"]},
        ],
    },
}

# Artikel, deren Titel eines dieser Wörter enthält, werden übersprungen
# (gegen Guides/Tier-Lists, die keine News sind). Groß/klein egal.
EXCLUDE_KEYWORDS = [
    "tier list", "how to", "guide", "best build", "counter", "codes", "crosshair",
    "betting", "casino", "gambling",
    "lpl financial", "stock", "wealth management", "investor", "nasdaq", "nyse",
    " plc", "returns chart", "share price", "dividend", "market cap",
    "how to watch", "watch live", "live stream", "livestream", "schedule and results", "stream schedule",
]

# Titel mit weniger Wörtern werden ignoriert (z. B. Video-Seiten, die nur "VOD" heißen)
MIN_TITLE_WORDS = 4

# Artikel dieser Seiten werden ignoriert (Finanz-/Börsenseiten: "VCT", "LPL" usw. sind dort Aktienkürzel)
EXCLUDE_SOURCES = [
    "yahoo finance", "marketscreener", "investing.com", "marketbeat", "seeking alpha", "morningstar",
    "zacks", "simply wall st", "stocktitan", "tipranks", "benzinga", "the motley fool", "finanzen.net",
    "marketwatch", "proactive investors", "sharecast", "lse.co.uk",
]

# KI-Zusammenfassung mit Google Gemini (kostenlos). Wird nur genutzt, wenn das
# GitHub-Secret GEMINI_API_KEY gesetzt ist. Die Modelle werden der Reihe nach probiert,
# falls eines kein kostenloses Kontingent (mehr) hat.
GEMINI_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
    "gemini-flash-latest",
]
GEMINI_PAUSE_SECONDS = 15   # Pause zwischen KI-Anfragen (Gratis-Stufe: nur wenige pro Minute)

# Hat die News-Seite kein Bild, sucht der Bot ein passendes (Spieler, Team, Liga; sonst Riot-Bild)
FALLBACK_IMAGES = True
# Kontakt für Liquipedia (verlangt, dass sich Bots mit einer Kontaktmöglichkeit ausweisen)
CONTACT = "https://github.com/snakebitepray-cmd/esports-news-bot"

# Bilder aus Artikeln nur von diesen Seiten übernehmen (andere nutzen oft zufällige Stockfotos)
TRUSTED_IMAGE_SITES = [
    "sheepesports.com", "dexerto.com", "dotesports.com", "esports.gg", "lolesports.com",
    "leagueoflegends.com", "playvalorant.com", "valorantesports.com", "vlr.gg", "liquipedia.net",
    "invenglobal.com", "egamersworld.com", "theesportsadvocate.com", "upcomer.com", "esportsinsider.com",
    "thespike.gg", "jaxon.gg", "win.gg", "oneesports.gg", "hltv.org", "redbull.com",
]

SUMMARY_LENGTH = 500       # ungefähre Länge der Zusammenfassung in Zeichen
# Spielplan (in die Esports-Kanäle)
TIMEZONE = "Europe/Berlin"
DAILY_SCHEDULE = True          # jeden Tag eine Übersicht der Matches der nächsten 24 Stunden
DAILY_SCHEDULE_HOUR = 10       # ab dieser Uhrzeit wird die Tagesübersicht gepostet
MATCH_REMINDERS = False        # True = Erinnerung kurz vor jedem Match
REMINDER_MINUTES = 35          # so viele Minuten vorher erinnern (Bot läuft alle 30 Minuten)

MAX_POSTS_PER_SOURCE = 5   # pro Durchlauf, damit der Kanal nicht geflutet wird
MAX_AGE_HOURS = 72         # ältere Artikel werden ignoriert
