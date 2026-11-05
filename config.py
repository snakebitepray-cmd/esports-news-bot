"""Hier stellst du ein, welche Quellen in welchen Kanal gepostet werden.

Typen:
  "riot" – offizielle Riot-Newsseite (leagueoflegends.com / playvalorant.com)
  "rss"  – beliebiger RSS/Atom-Feed

"label" erscheint über jeder Nachricht, damit man Patch-Notes und Esports-News
unterscheiden kann. Quellen kannst du einfach hinzufügen oder auskommentieren.
"""

# Sprache der offiziellen Riot-Seiten (z. B. "de-de" oder "en-us")
RIOT_LOCALE = "de-de"


def google_news(query: str) -> str:
    """Google-News-Suche als RSS-Feed (deutsche Ergebnisse)."""
    from urllib.parse import quote
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=de&gl=DE&ceid=DE:de"


GAMES = {
    "lol": {
        "name": "League of Legends",
        "webhook_env": "LOL_WEBHOOK_URL",  # GitHub-Secret mit der Webhook-URL
        "color": 0xC89B3C,
        "sources": [
            {"type": "riot", "label": "🛠️ Update",
             "url": f"https://www.leagueoflegends.com/{RIOT_LOCALE}/news/game-updates/"},
            {"type": "riot", "label": "🏆 Esports",
             "url": f"https://www.leagueoflegends.com/{RIOT_LOCALE}/news/esports/"},
            {"type": "rss", "label": "🏆 Esports",
             "url": "https://dotesports.com/league-of-legends/feed"},
            {"type": "rss", "label": "🏆 Esports",
             "url": google_news('"LoL Esports" OR LEC OR "League of Legends Worlds" OR "Prime League" when:2d')},
        ],
    },
    "valo": {
        "name": "Valorant",
        "webhook_env": "VALO_WEBHOOK_URL",
        "color": 0xFF4655,
        "sources": [
            {"type": "riot", "label": "🛠️ Update",
             "url": f"https://playvalorant.com/{RIOT_LOCALE}/news/game-updates/"},
            {"type": "rss", "label": "🏆 Esports",
             "url": "https://dotesports.com/valorant/feed"},
            {"type": "rss", "label": "🏆 Esports",
             "url": google_news('"Valorant Champions Tour" OR VCT OR "Valorant Esports" when:2d')},
        ],
    },
}

# Artikel, deren Titel eines dieser Wörter enthält, werden übersprungen
# (gegen Guides/Tier-Lists, die keine News sind). Groß/klein egal.
EXCLUDE_KEYWORDS = [
    "tier list", "how to", "guide", "best build", "counter", "codes", "crosshair",
]

MAX_POSTS_PER_SOURCE = 5   # pro Durchlauf, damit der Kanal nicht geflutet wird
MAX_AGE_HOURS = 72         # ältere Artikel werden ignoriert
