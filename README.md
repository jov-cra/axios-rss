# Axios (Tech, AI & Business)

Ein persönlicher Axios-RSS-Feed, der nur die Tech-, AI- und Wirtschaftsseite von Axios durchlässt: AI, Technologie, Business, Economy, Energie/Klima, Health, Science. Politik, World, Defense und Gesellschaft (Kriminalität, Religion, Sport, Promis) fliegen raus. Läuft per GitHub Actions, erzeugt eine `feed.xml`, die du in **Tapestry** o. Ä. abonnierst.

Seit v5 (17.10.2026) ist der Filter eine **Whitelist**: jedes Item wird einem Axios-Ressort zugeordnet, veröffentlicht werden nur die Ressorts in `AX_KEEP_DESKS`. Entscheidend ist, worum es geht, nicht wer handelt: ein Politiker zu AI ist eine AI-Meldung.

## Warum ein Klassifikator?

Der Axios-Feed taggt Items nur mit `<category>top</category>` (Prominenz, kein Thema). Das echte Ressort steht ausschließlich auf der Artikelseite — und die **blockt Cloudflare für CI-IPs (403)**. Axios hat auch **keine** nativen Themen-Feeds mehr (alle Kandidaten 404/403, geprüft). Es gibt also **kein server-geliefertes Themen-Signal**, das von GitHub aus erreichbar ist.

Deshalb wird jedes Item aus **Titel + Kurzbeschreibung** klassifiziert — mit dem **günstigsten Modell (Haiku 5.5)**, Antwort als festes JSON-Schema:

1. Axios-Feed ziehen.
2. Für jedes **neue** Item einmal nach dem Ressort fragen (bei Grenzfällen zweimal, das Behalten gewinnt); Ressort pro `<guid>` **gecacht** (News-Items ändern ihr Ressort nie) → laufende Kosten nur für neue Artikel, Cent-Bereich/Monat.
3. Items außerhalb der behaltenen Ressorts rauswerfen; **byte-treu** neu zusammensetzen (Bilder, `content:encoded`, Autoren bleiben erhalten).
4. Ein Item, das in diesem Lauf nicht klassifiziert werden konnte, bleibt draußen und wird im nächsten Lauf neu gefragt.

## Setup

1. Dateien in ein **öffentliches** Repo, *Settings → Pages → Deploy from branch `main` / root*.
2. **API-Key als Secret hinterlegen:** *Settings → Secrets and variables → Actions → New repository secret* → Name `ANTHROPIC_API_KEY`, Wert = dein Anthropic-Key. (Der Key wird nur als Secret gespeichert, taucht nie im Code/Log auf.)
3. Der Workflow läuft alle 30 Min und committet `feed.xml` + `state.json`.

Feed abonnieren:
```
https://jov-cra.github.io/axios-rss/feed.xml
```

**Fail-closed:** ohne Secret, bei ungültigem `AX_KEEP_DESKS`, wenn mehr als 30 % der Klassifikationen scheitern oder nichts übrig bleibt **bricht der Lauf ab** und lässt den letzten guten Feed stehen, statt einen halb geprüften Feed auszuliefern. Manuelle Korrekturen: `AX_FORCE_KEEP` / `AX_FORCE_DROP` (Komma-Liste von Substrings in guid/Titel).

## Konfiguration (Workflow-`env:`)

| ENV | Default | Bedeutung |
|-----|---------|-----------|
| `ANTHROPIC_API_KEY` | – (Secret) | Anthropic-Key für die Klassifikation |
| `AX_MODEL` | `claude-haiku-5-5` | günstigstes Modell (Modellwechsel klassifiziert neu) |
| `AX_KEEP_DESKS` | `ai,technology,business,economy,energy_climate,health,science` | behaltene Ressorts; weitere: `politics`, `world`, `defense`, `society` |
| `AX_MAX_FAIL_RATIO` | `0.3` | Abbruch, wenn mehr Klassifikationen scheitern |
| `AX_TITLE` | `Axios (Tech, AI & Business)` | Feed-Titel |
| `AX_FEED_URL` | `https://api.axios.com/feed/` | Quell-Feed |
| `AX_FEED_SELF` | – | öffentliche Feed-URL (atom:self) |
| `AX_CLASSIFY_MAX` | `300` | max. API-Aufrufe pro Lauf |

Die Ressorts, ihre Abgrenzung und die Beispiele stehen in `DESKS`, `SYSTEM` und `FEW_SHOTS` in `axios_filter.py`; `PROMPT_VERSION` hochzählen erzwingt Neu-Klassifikation. Vor einer Änderung auf einem Branch `v5-*` pushen: der Workflow *Evaluate classifier* lässt die neue Fassung über `tests/fixtures/evalset.tsv` (318 frühere Items) und den aktuellen Feed laufen und lädt das Ergebnis als Artefakt hoch.

## Tests

```bash
pip install -r requirements.txt
python tests/test_filter.py
```
Alles offline (Klassifikator gemockt): Feed-Zerlegung, Ressort-Filter, Antwort-Validierung, Abbruchregeln, Cache-Migration, Head-Anpassung und byte-identische Ausgabe (kein Commit-Churn).

## v5, 17.10.2026: Whitelist statt Politik-Blacklist

- **Anlass:** v4 warf nur Politik raus und ließ alles andere durch (Papst, Hinrichtungen, Sport), und seine Ausnahmen („im Zweifel behalten", Kosten- und Privatgeschichten) ließen Politik durch. Jetzt wird jedes Item einem von elf Axios-Ressorts zugeordnet (an Axios' Newslettern und Rubriken ausgerichtet), behalten werden nur die Ressorts in `AX_KEEP_DESKS`. Wer handelt, spielt keine Rolle.
- **Haiku 5.5 mit Structured Output** (Ressort als enum), `effort: low`, `max_tokens` 2048, weil das Denken mitzählt. Nur `end_turn` zählt als Antwort; zwei Verweigerungen hintereinander werden als `refused` gecacht und nie veröffentlicht.
- **Zweite Frage nur bei Grenzfällen:** markiert das Modell ein verworfenes Item selbst als Grenzfall, wird ein zweites Mal gefragt und ein behaltenes Ressort gewinnt. Die pauschale Bestätigungsfrage von v4 ist weg.
- **Strengeres Fail-closed:** Abbruch schon bei mehr als 30 % gescheiterten Klassifikationen und bei leerem Ergebnis; ein nicht klassifiziertes Item bleibt draußen statt durchzurutschen. Alte v4-Urteile gelten nicht weiter, ein Modellwechsel klassifiziert neu.
- **Chart-Auflösung wird gemerkt:** die Wahl zwischen `fallback.png` und `full.png` steht im State, damit die Feed-Bytes nicht mit einer wackeligen Netzanfrage kippen.

## Härtung 27.08.2026 (v4, abgelöst)

- **Verwerfen muss bestätigt werden.** Das Urteil wird pro `guid` **dauerhaft** gecacht, und die Messages-API kennt kein `temperature`/`top_p`/`top_k` mehr — ein einmaliger Sampling-Ausrutscher wäre also für immer eingefroren. Ein *keep* gilt nach einer Antwort, ein *drop* wird ein zweites Mal gefragt; Uneinigkeit löst sich Richtung behalten auf. Das ist genau die erklärte Präferenz (lieber ein Politik-Artikel durchrutschen als über-filtern) und kostet nur bei Treffern eine zweite Anfrage.
- **Strikte Antwort-Validierung.** Alles außer `yes`/`no` wirft jetzt, statt still als „behalten" im Cache zu landen. Ein API-Glitch wird im nächsten Lauf erneut versucht, nicht für immer festgeschrieben.
- **Chart landet in beiden Bodies.** Der injizierte `<img>` geht in `content:encoded` **und** in `<description>` — Readwise rendert `<description>`, Tapestry `content:encoded`. Vorher sah die Hälfte der Reader den Chart nicht.
- **Prompt `v4`:** schärfere Grenze „Politiker-Handeln vs. Business-Framing". Kosten-/Verbraucherstorys und die wirtschaftlichen Folgen von Zöllen und Handel sind **keine** Politik — nur der politische Kampf um die Policy ist es; Privates und Triviales um einen Politiker ebenfalls nicht. Wie eine Partei oder Kampagne sich inszeniert und um Wähler wirbt, **ist** dagegen Politik, auch wenn das Vehikel Social Media oder ein Meme ist (das musste v4 an v3 nachschärfen: „Democrats enter their thirst trap era" war unter v3 durchgerutscht).
- **Roter Lauf macht sich bemerkbar:** der Workflow legt bei `failure()` ein Issue an (bzw. kommentiert das offene). Fail-closed heißt sonst: der alte Feed wird weiter ausgeliefert und niemand merkt, dass er altert.
