# ADR 0036: Run-Retry mit Backoff und Ablage erschöpfter Aufträge

**Status:** Akzeptiert (2026-10-23; Draft 2026-10-21, überarbeitet 2026-10-22 nach Review in PR #291 und PR #295);
Amendment 2026-10-26 (Akzeptiert 2026-10-28 mit PR #373): `max_wait`, kanonische Trigger-Schlüssel, Store-Vertrag für
Eindeutigkeit und Claim — die Review-Punkte aus PR #309, die mit dem geschlossenen PR #295 nicht auf `main` kamen
**Umsetzung:** #290 (blockiert durch #289, #258)
**Baut auf:** ADR 0035 (Fehlerklassifikation).
**Ergänzt:** ADR 0012 (Retry *innerhalb* eines Steps), ADR 0025 (Alert-Dedup), ADR 0034 (Run-Modell;
neue Felder siehe Entscheidung 8).
**Ersetzt teilweise:** ADR 0014, Abschnitt „Kein Auto-Retry“ (siehe Entscheidung 7).

## Kontext

`reconcile` zählt jeden Run als erledigt, auch `fail`/`error`; Retry ist manuell (ADR 0014).
Stand 2026-10-21 gab es **69 Issue-/PR-Trigger**, deren sämtliche Runs gescheitert waren und die nie
wiederholt wurden, überwiegend `develop`. Ein Teil scheiterte nur an Neustarts, Netzfehlern oder
hängenden Agent-Sessions (ADR 0035: `interrupted`, `transient`). Diese Aufträge fallen still aus
dem Prozess.

*Datenbasis:* Run-Records unter `runs/*/*.json`, gruppiert nach `(project, loop, trigger)` für
`on_issue:*`/`on_pr:*`; gezählt werden Gruppen, in denen jeder Run `fail` oder `error` ist. Die
Zählung lief im Laufe des 2026-10-21 (1 230 Records → 69); dieselbe Abfrage auf dem Stand zum
Tagesende ergibt 1 246 Records → 72 (40× `develop`).

ADR 0014 lehnte Auto-Retry wegen der Gefahr einer Endlosschleife bei dauerhaft kaputten Issues ab.
Mit der Fehlerklassifikation lässt sich diese Gefahr gezielt ausschließen.

## Begriffe

- **Run 0:** der reguläre Run eines Triggers (Poll, Webhook, Cron-Tick, manuell).
- **Versuch *n*** (`attempt = n`, 1 ≤ *n* ≤ `max`): die *n*-te automatische Wiederholung.
- **Kette:** der Wiederholungszustand eines Auftrags `(Projekt, Loop, Trigger)`. `Trigger` ist der
  **stabile Schlüssel** der Auslösequelle, **nicht** die ID des einzelnen Ticks, Events oder
  Webhook-Zustellung (Delivery-Id). Weitere Cron-Ticks tragen daher denselben Schlüssel.
  Kanonische Form (Felder, die über die Lebensdauer des Auftrags unveränderlich sind):

  | Quelle | Schlüssel |
  |---|---|
  | Cron | `cron:<schedule-id>` (Id des Schedules in der Spec, nicht der Tick) |
  | Issue (Poll/Webhook) | `issue:<owner>/<repo>#<nummer>` |
  | PR (Poll/Webhook) | `pr:<owner>/<repo>#<nummer>` |
  | Event ohne Issue-/PR-/Schedule-Bezug | kein Schlüssel → **keine Kette**, Fehlschlag alarmiert wie bisher |

  Die Lebensdauer des Schlüssels endet mit der Kette (terminaler Zustand, Entscheidung 4); ein
  späterer Fehlschlag desselben Objekts eröffnet eine neue Kette. Verschiedene Repositories oder
  Nummern kollidieren nicht, Wiederholungen desselben Objekts finden dieselbe Kette.

## Entscheidung

1. **Retry-Policy pro Loop, Default aus.** Spec-Feld `retry` auf Projekt- oder Loop-Ebene. Ein
   Loop-`retry` ersetzt den Projekt-Block vollständig (kein Feld-Merge); `max: 0` auf Loop-Ebene
   schaltet eine Projekt-Policy für diesen Loop ab.

   ```yaml
   retry:
     max: 3                      # zusätzliche Versuche nach Run 0 → höchstens 4 Runs
     backoff: [5m, 30m, 2h]      # Wartezeit vor Versuch 1, 2, 3 (letzter Wert wiederholt sich)
     "on": [interrupted, transient]
     max_wait: 24h               # Obergrenze für retry_after (siehe unten)
   ```

   **Validierung** (Spec-Fehler, wenn nicht anders angegeben):

   | Feld | Regel | fehlt |
   |---|---|---|
   | `max` | ganze Zahl ≥ 0; `0` = kein Retry | `3` |
   | `backoff` | nicht-leere Liste von Dauern > 0 (`45s`, `5m`, `2h`, `1d`). Weniger Einträge als `max`: der letzte gilt für alle weiteren. Mehr Einträge als `max`: **Warnung** (wirkungslos). | `[5m, 30m, 2h]` |
   | `max_wait` | Dauer > 0 (Format wie `backoff`); Obergrenze für die Wartezeit aus `retry_after`, gemessen ab Ende des vorherigen Runs. Gilt für alle Versuche der Kette. | `24h` |
   | `on` | nicht-leere Teilmenge von `interrupted`, `transient`; Duplikate sind wirkungslos. `environment`, `outcome`, `unknown` werden abgelehnt. | beide |
   | sonst | unbekannte Schlüssel werden abgelehnt | |

   Wartezeit vor Versuch *n*: `backoff[min(n, len(backoff)) − 1]`, gemessen ab Ende des
   vorherigen Runs. Meldet der gescheiterte Run ein `retry_after` (ADR 0035, z. B. Reset-Zeit
   eines LLM-Limits), gilt der spätere der beiden Zeitpunkte:
   `next_attempt_at = max(Ende + Wartezeit n, min(retry_after, Ende + max_wait))`. Ohne
   `retry`-Block (Run 0, noch keine Kette) gilt für die Kappung nach ADR 0035 der Default `24h`.
   So verbrauchen Versuche innerhalb
   eines bekannten Limits kein Budget, und Runs, die am selben Limit gescheitert sind, warten
   gemeinsam auf den Reset (gleichzeitig starten sie dann nur im Rahmen des Worker-Pools).

   **YAML-Falle:** PyYAML (YAML 1.1) liest den unquotierten Schlüssel `on` als Boolean `True`. Der
   Parser behandelt `True` als `on`, solange `on` nicht zusätzlich gesetzt ist; Beispiele und
   Projekt-Specs quoten `"on"`. Parsing und Semantik liegen in genau einem Modul
   (`src/fabrikant/retry.py`, `RetryPolicy`), das auch der Relay-Entwurf (PR #293) nutzt.

2. **Cron-Loops werden wie alle anderen wiederholt.** Der Retry folgt dem Backoff, nicht dem
   nächsten Tick; bei einem wöchentlichen Job ginge sonst eine Woche verloren. Damit Retry und Tick
   nicht doppelt laufen:

   - Solange für den Loop eine Kette `pending` ist, wird ein fälliger Cron-Tick **übersprungen**
     (Log-Eintrag, kein Run); die Kette erledigt dieselbe Arbeit. Ein übersprungener Tick wird
     nicht nachgeholt.
   - Validierung gegen das Cron-Intervall (kürzester Abstand zweier Ticks):
     ein wirksamer `backoff`-Wert ≥ Intervall ist ein **Fehler** (der Tick wäre früher als der
     Retry); dauert die ganze Kette (Summe der ersten `max` Wartezeiten) länger als das Intervall,
     ist das eine **Warnung** (Ticks werden übersprungen).

3. **Retry-Ketten sind eigene Datensätze, keine neuen Run-Status.** Eine Kette hält
   `(Projekt, Loop, Trigger)`, `state`, `next_attempt_at` und `last_run_id` (Run-Id des letzten
   Runs der Kette). Mehr nicht: Versuchszahl und Historie stehen an den Runs (`attempt`,
   `retry_of`, Entscheidung 8) und werden über `last_run_id` → `retry_of` → … abgeleitet. Je
   Schlüssel gibt es höchstens eine Kette im Zustand `pending` und je Kette höchstens einen
   laufenden Versuch; beides garantiert der Store (ADR 0034), nicht der Scheduler allein:

   - **Eindeutigkeit:** Unique-Key `(project, loop, trigger_key)` auf Ketten mit `state =
     'pending'` (SQL: partieller Unique-Index; JSON: Prüfung innerhalb derselben Sperre).
   - **Claim:** Eine Kette trägt zusätzlich `claimed_run_id` (`NULL` = frei). Einen fälligen
     Versuch startet nur, wer in einer einzigen Transaktion `claimed_run_id` von `NULL` auf die
     neue Run-Id setzt, bedingt auf `state = 'pending'` und `next_attempt_at <= now` (SQL:
     `UPDATE … WHERE … RETURNING`; genau eine betroffene Zeile gewinnt). Wer 0 Zeilen trifft,
     startet nichts.
   - **Freigabe:** Das Ende des Versuchs setzt `claimed_run_id` zurück und aktualisiert Zustand und
     `next_attempt_at` in derselben Transaktion. Nach einem Neustart gibt der Reaper (ADR 0035)
     den Claim eines nicht mehr laufenden Runs frei, bevor der Scheduler fällige Versuche startet.
   - **JSON-Phase:** Lesen–Prüfen–Schreiben liegt vollständig unter der exklusiven Dateisperre auf
     `state/retry-chains.json` (prozessübergreifend, `flock`); geschrieben wird atomar (temporäre
     Datei + `rename`). Claim und Eindeutigkeit sind damit dieselben Operationen wie im SQL-Store
     und werden von dort unverändert übernommen.

   Beendete Ketten bleiben als Historie, ein späterer Fehlschlag desselben Schlüssels (z. B. nächster Cron-Tick) eröffnet eine
   neue. Der Zustandsautomat der Runs (ADR 0034) bleibt unverändert.

4. **Zustandsautomat der Kette** (`pending → exhausted | resolved | dismissed`, `exhausted →
   pending | resolved | dismissed`):

   | Ereignis | Wirkung |
   |---|---|
   | Run 0 scheitert mit Klasse in `on`, `max > 0` | neue Kette `pending`, `next_attempt_at` für Versuch 1 (Entscheidung 1) |
   | Versuch *n* scheitert mit Klasse in `on`, *n* < `max` | bleibt `pending`, `next_attempt_at` für Versuch *n*+1 (Entscheidung 1) |
   | Versuch `max` scheitert, **oder** ein Versuch scheitert mit Klasse außerhalb von `on` | `exhausted` |
   | ein Run desselben Schlüssels endet `pass` (Versuch, Cron-Tick, manuell) | `resolved` |
   | ein Versuch endet am Approval-Gate `rejected` | `dismissed` (menschliche Entscheidung) |
   | Trigger gilt nicht mehr (Issue geschlossen, PR gemergt/geschlossen) | `dismissed` |
   | UI *verwerfen* (aus `pending` oder `exhausted`) | `dismissed`, `next_attempt_at` gelöscht; ein laufender Versuch läuft zu Ende, plant aber nichts mehr |
   | UI *jetzt erneut* (nur aus `exhausted`) | startet sofort einen Run mit `attempt = 0`, `retry_of = last_run_id`; Kette wieder `pending` mit vollem Budget |

   Ein Server-Neustart *während* eines Versuchs ist der Kernfall: Der Reaper beendet den Run als
   `interrupted` (ADR 0035), das verbraucht den Versuch, und der nächste wird nach Backoff geplant.
   Ein vom Menschen abgebrochener Run (offene Frage `canceled` in ADR 0035) setzt die Kette auf
   `dismissed`, sobald es den Status gibt.

5. **Der Scheduler startet fällige Versuche**, auch nach einem Neustart (wie
   `requeue_queued_runs`); überfällige Versuche starten sofort, je Kette genau einer (der Claim aus
   Entscheidung 3 gelingt für genau einen Starter). Ein Versuch
   durchläuft alle Gates neu (Approval, Blocker). Bei pausiertem oder deaktiviertem Loop wartet ein
   fälliger Versuch bis zur Freigabe und verbraucht kein Budget.

6. **Ablage und Alerts.**
   - `pending`: kein `run_failed`-Alert für Runs der Kette. Sichtbar am Run (Liste, Modal:
     „Versuch *n*/`max`, nächster um …“) mit Aktion *verwerfen*.
   - `exhausted`: genau ein Event `run_exhausted` über die Alert-Kanäle (Dedup nach ADR 0025) mit
     Klasse und Fehler des letzten Runs; `environment` wird hervorgehoben (ADR 0035). Eintrag in der
     UI-Liste „Abgelegt“ (analog Freigaben) mit Klasse, letztem Fehler, Versuchszahl und den
     Aktionen *jetzt erneut* und *verwerfen*.
   - `dismissed` und `resolved`: kein Alert. *Verwerfen* ist eine bewusste Quittung in der UI, in
     der der letzte Fehler sichtbar war; es geht also kein Alert still verloren.
   - Fehlschläge ohne Kette (Loop ohne `retry`, Klasse nicht in `on` bei Run 0) alarmieren wie
     bisher mit `run_failed`.

7. **ADR 0014 bleibt im Kern gültig:** `reconcile` startet keinen zweiten Run für einen Trigger,
   der schon einen hatte. Wiederholt wird ausschließlich über die Retry-Policy, begrenzt durch
   `max`. Es entfällt nur die Aussage „Retry bleibt manuell“.

8. **Persistenz und Reihenfolge.** #290 erweitert das Run-Modell (ADR 0034) um die optionalen
   Felder `retry_of` (Run-Id des vorherigen Runs der Kette) und `attempt` (0 = Run 0 bzw.
   *jetzt erneut*); Altbestand liest sich als `None`/`0`. Ketten liegen bis zum SQL-Store (#277)
   als JSON unter `state/` (Dateisperre wie die übrigen `state/`-Dateien). Landet #277 zuerst,
   kommen Felder und Ketten-Tabelle per Migration hinein, sonst gleich ins Schema; der Import
   (ADR 0034, Schritt 4) übernimmt die JSON-Ketten.

9. **Voraussetzung:** #258 (Reconcile dedupliziert eingereihte und pausierte Runs). Retry reiht
   Runs ein und würde die dort beschriebene Doppel-Run-Lücke sonst verschärfen.

## Begründung

- **Begrenzt und gezielt:** Nur Klassen, bei denen ein Wiederholen plausibel etwas ändert, und
  höchstens `max` zusätzliche Runs je Kette. Ein Versuch mit anderer Klasse beendet die Kette
  sofort. Die Endlosschleife aus ADR 0014 ist damit ausgeschlossen.
- **Opt-in pro Loop:** Nicht jeder Loop ist idempotent. Ein abgebrochener `develop`-Run kann einen
  Branch oder PR hinterlassen haben; der Loop muss das vertragen, bevor er `retry` bekommt.
- **Ketten statt Run-Status:** Retry ist eine Eigenschaft des Auftrags, nicht eines Runs. Das hält
  den Zustandsautomaten aus ADR 0034 klein. Die Kette speichert nur, was nicht aus den Runs
  ableitbar ist.
- **Cron ohne Sonderfall:** Ein Regelwerk für alle Trigger; die Intervall-Validierung fängt die
  Fälle ab, in denen Retry und Tick kollidieren würden.
- **Nichts fällt still heraus:** Was nicht heilt, landet sichtbar in einer Liste mit Aktion.

## Abgelehnte Alternativen

- **Retry ohne Klassifikation:** wiederholt deterministische Fehler n-mal und verzögert den Alert
  um n × Backoff.
- **Run-Status `retrying`/`exhausted`:** vermischt Auftrag und Ausführung, vergrößert den
  Zustandsautomaten ohne Not.
- **Retry über `reconcile`** (gescheiterte Runs nicht als erledigt zählen): kein Backoff, kein
  Limit, keine Ablage; genau die Endlosschleife aus ADR 0014.
- **Bei Cron ist der nächste Tick der Retry:** bei langen Intervallen (wöchentlich) geht ein
  ganzer Lauf verloren, gerade bei `transient`-Fehlern, die nach Minuten heilen.
- **Liste der Run-Ids in der Kette:** redundant zu `retry_of`/`attempt` an den Runs; zwei Quellen,
  die auseinanderlaufen können.
- **Auf #19 warten (Postgres-Queue mit DLQ):** Plattform-Umbau in Phase 2, der Verlust passiert
  jetzt. Die Ketten lassen sich später in Store bzw. Queue übernehmen.

## Konsequenzen

- (+) Abgebrochene und transient gescheiterte Trigger heilen sich selbst, auch über Neustarts.
- (+) Erschöpfte Aufträge sind sichtbar und mit einem Klick wieder anstoßbar.
- (−) Loops mit `retry` müssen idempotent sein (Branch/PR schon vorhanden, Kommentar schon
  gepostet). `develop` bekommt `retry` erst, wenn das geklärt ist.
- (−) Loops mit Approval-Gate stellen je Versuch eine neue Freigabe-Anfrage (bis zu `max` + 1 für
  denselben Trigger). Für solche Loops `max` klein halten.
- (−) Cron-Ticks entfallen, solange eine Kette läuft.
- (−) Ein weiterer persistenter Zustand (Ketten) neben den Runs; wandert mit #277 in den Run-Store.

