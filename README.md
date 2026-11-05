# Esports-News-Bot (LoL & Valorant)

Postet automatisch neue **Patch-Notes/Updates** und **Esports-News** in zwei Discord-Kanäle:
`#lol-news` bekommt nur League of Legends, `#valo-news` nur Valorant.
Läuft **kostenlos rund um die Uhr** über GitHub Actions (alle 30 Minuten), ohne eigenen Server.

## Einrichtung (ca. 10 Minuten)

### 1. Webhooks in Discord erstellen
Für **jeden** der beiden Kanäle:
Kanal-Einstellungen ⚙️ → **Integrationen** → **Webhooks** → **Neuer Webhook** → Name & Bild setzen (z. B. „LoL News“) → **Webhook-URL kopieren**.

### 2. GitHub-Repository anlegen
1. Kostenlosen Account auf https://github.com erstellen.
2. **New repository** → Name z. B. `esports-news-bot` → **Public** (dann sind Actions unbegrenzt gratis) → Create.
3. **Add file → Upload files** → alle Dateien aus diesem Ordner hochladen
   (auch den Ordner `.github/workflows/`; falls der Upload ihn nicht mitnimmt: **Add file → Create new file**, als Namen `.github/workflows/news.yml` eintippen und den Inhalt reinkopieren).

> Die Webhook-URLs kommen **nicht** in den Code, sondern in Secrets. Dadurch sind sie auch bei einem öffentlichen Repo geschützt.

### 3. Secrets eintragen
Im Repo: **Settings → Secrets and variables → Actions → New repository secret**
- `LOL_WEBHOOK_URL` = Webhook-URL des LoL-Kanals
- `VALO_WEBHOOK_URL` = Webhook-URL des Valorant-Kanals

### 4. Testen & starten
**Actions** → „Esports News“ → **Run workflow**
- Mit Haken bei *Test-Modus*: zeigt im Log, was gefunden würde, ohne etwas zu posten.
- Ohne Haken: echter Lauf. Beim ersten Mal wird pro Quelle nur der neueste Artikel gepostet, danach nur noch neue.

Ab dann läuft alles automatisch.

## Anpassen
Alles in `config.py`:
- Quellen hinzufügen/entfernen (jeder RSS-Feed funktioniert)
- `RIOT_LOCALE` = `"en-us"` für englische Patch-Notes
- `EXCLUDE_KEYWORDS`: Titel mit diesen Wörtern werden ignoriert (z. B. Guides)

Intervall ändern: `cron` in `.github/workflows/news.yml`.

## Gut zu wissen
- GitHub startet geplante Läufe manchmal ein paar Minuten verspätet. Für News ist das kein Problem.
- Bei **privatem** Repo: 2000 Gratis-Minuten pro Monat, alle 30 Min. passt da knapp rein. Öffentlich = unbegrenzt.
- Wenn eine Quelle ausfällt, steht das im Actions-Log (❌). Die anderen Quellen laufen weiter.
