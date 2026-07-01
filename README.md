# Axios (no Politics)

Ein persönlicher, **politikfreier** Axios-RSS-Feed — Politik raus, alles andere bleibt drin. Läuft per GitHub Actions, erzeugt eine `feed.xml`, die du in **Tapestry** o. Ä. abonnierst.

## Warum ein Klassifikator?

Der Axios-Feed taggt Items nur mit `<category>top</category>` (Prominenz, kein Thema). Das echte Ressort steht ausschließlich auf der Artikelseite — und die **blockt Cloudflare für CI-IPs (403)**. Axios hat auch **keine** nativen Themen-Feeds mehr (alle Kandidaten 404/403, geprüft). Es gibt also **kein server-geliefertes Themen-Signal**, das von GitHub aus erreichbar ist.

Deshalb wird jedes Item aus **Titel + Kurzbeschreibung** klassifiziert — mit dem **günstigsten Modell (Haiku)**:

1. Axios-Feed ziehen.
2. Für jedes **neue** Item einmal „Politik? ja/nein" fragen; Ergebnis pro `<guid>` **gecacht** (News-Items ändern ihr Ressort nie) → laufende Kosten nur für neue Artikel, Cent-Bereich/Monat.
3. Politik-Items rauswerfen; **byte-treu** neu zusammensetzen (Bilder, `content:encoded`, Autoren bleiben erhalten).
4. Bei Fehler/fehlendem Key: Item wird **behalten** (nie droppen bei Unsicherheit).

## Setup

1. Dateien in ein **öffentliches** Repo, *Settings → Pages → Deploy from branch `main` / root*.
2. **API-Key als Secret hinterlegen:** *Settings → Secrets and variables → Actions → New repository secret* → Name `ANTHROPIC_API_KEY`, Wert = dein Anthropic-Key. (Der Key wird nur als Secret gespeichert, taucht nie im Code/Log auf.)
3. Der Workflow läuft alle 30 Min und committet `feed.xml` + `state.json`.

Feed abonnieren:
```
https://jov-cra.github.io/axios-filtered/feed.xml
```

**Ohne Secret** filtert nichts — der Feed läuft dann als unveränderter Axios-Feed weiter, bis der Key da ist.

## Konfiguration (Workflow-`env:`)

| ENV | Default | Bedeutung |
|-----|---------|-----------|
| `ANTHROPIC_API_KEY` | – (Secret) | Anthropic-Key für die Klassifikation |
| `AX_MODEL` | `claude-haiku-4-5-20251001` | günstigstes Modell |
| `AX_TITLE` | `Axios (no Politics)` | Feed-Titel |
| `AX_FEED_URL` | `https://api.axios.com/feed/` | Quell-Feed |
| `AX_FEED_SELF` | – | öffentliche Feed-URL (atom:self) |
| `AX_CLASSIFY_MAX` | `150` | max. Klassifikationen pro Lauf |

Was als „Politik" zählt, steht im `PROMPT` in `axios_filter.py` (US-Politik/Politics & Policy; Business/Tech/Economy/… gelten NICHT als Politik, auch wenn ein Politiker vorkommt). Trivial anpassbar.

## Tests

```bash
pip install -r requirements.txt
python tests/test_filter.py
```
Alles offline (Klassifikator gemockt): Feed-Zerlegung, Politik-Drop, Head-Anpassung, „ohne Key nichts droppen" und byte-identische Ausgabe (kein Commit-Churn).
