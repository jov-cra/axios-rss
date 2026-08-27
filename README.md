# Axios (no Politics)

Ein persönlicher, **politikfreier** Axios-RSS-Feed — **US-Politik und World/Außenpolitik raus**, alles andere (Business, Tech, Economy, Science, Health, …) bleibt drin. Läuft per GitHub Actions, erzeugt eine `feed.xml`, die du in **Tapestry** o. Ä. abonnierst.

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
https://jov-cra.github.io/axios-rss/feed.xml
```

**Fail-closed:** ohne Secret (oder bei systematischem Klassifikations-Ausfall) **bricht der Lauf ab** und lässt den letzten guten Feed stehen, statt still den ungefilterten Firehose auszuliefern. Einzelne transiente Fehler behalten nur das betroffene Item. Manuelle Korrekturen: `AX_FORCE_KEEP` / `AX_FORCE_DROP` (Komma-Liste von Substrings in guid/Titel).

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

## Härtung 27.08.2026

- **`temperature=0`** im Klassifikator. Das Urteil wird pro `guid` **dauerhaft** gecacht — mit der Default-Temperatur konnte derselbe Stoff zweimal gegensätzlich beurteilt und der Münzwurf dann eingefroren werden.
- **Strikte Antwort-Validierung.** Alles außer `yes`/`no` wirft jetzt, statt still als „behalten" im Cache zu landen. Ein API-Glitch wird im nächsten Lauf erneut versucht, nicht für immer festgeschrieben.
- **Chart landet in beiden Bodies.** Der injizierte `<img>` geht in `content:encoded` **und** in `<description>` — Readwise rendert `<description>`, Tapestry `content:encoded`. Vorher sah die Hälfte der Reader den Chart nicht.
- **Prompt `v3`:** schärfere Grenze „Politiker-Handeln vs. Business-Framing". Politiker-Memes, Kosten-/Verbraucherstorys und wirtschaftliche Folgen von Zöllen/Handel sind **keine** Politik; nur der politische Kampf um die Policy ist es.
- **Roter Lauf macht sich bemerkbar:** der Workflow legt bei `failure()` ein Issue an (bzw. kommentiert das offene). Fail-closed heißt sonst: der alte Feed wird weiter ausgeliefert und niemand merkt, dass er altert.
