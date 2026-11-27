# ADR 0035: Fehlerklassifikation gescheiterter Steps und Runs (`FailureKind`)

**Status:** Akzeptiert (2026-10-23; Draft 2026-10-21, überarbeitet 2026-10-22 nach Review in PR #291 und PR #295);
Amendment 2026-10-26 (Draft, PR #362): Kappung von `retry_after` konkret über `max_wait` (ADR 0036); references fixed 2026-11-26
**Umsetzung:** #289
**Ergänzt:** ADR 0012 (transiente Backend-Fehler im agent-Step), ADR 0025 (Alert-Dedup),
ADR 0034 (typisiertes Run-Modell).
**Grundlage für:** ADR 0036 (Run-Retry).

## Kontext

Ein gescheiterter Run ist heute nur `fail` oder `error` plus Freitext. Was die Ursache *bedeutet*,
weiß niemand im System; jede Stelle, die darauf reagieren will, müsste Fehlertexte parsen.
Auswertung der Run-Records am 2026-10-21 (1 230 Runs, alle Projekte; Abfrage siehe unten):

- **Abgebrochen oder Infrastruktur:** 11× Server-Neustart, 19× Dead-Man's-Switch (`stale`),
  6× `git fetch`-Netzfehler, ~34× Agent-Session ohne Antwort bzw. Backend `unhealthy`.
- **Umgebung kaputt:** 31× `pysonar: command not found`, Konfigurationsfehler wie
  `action 'list' ungültig`. Jeder dieser Runs sah aus wie ein normaler Fehlschlag; das Problem fiel
  erst nach Dutzenden Runs auf.
- **Echtes Ergebnis:** ~30× Tests rot, 12× Quality Gate `ERROR`, Review `changes_requested`.

Diese Gruppen verlangen völlig unterschiedliche Reaktionen (wiederholen, Mensch alarmieren,
nichts tun). Ohne Klassifikation kann weder die UI noch ein Alert noch ein späterer Mechanismus
(Retry, Routing) sie unterscheiden.

*Datenbasis:* je Run ein `runs/*/*.json` mit Status `fail`/`error`, Kategorien per Suche im
Fehlertext (z. B. `pysonar: command not found`, `Quality Gate`, `stale`). Die Zählung lief im Laufe
des 2026-10-21; dieselbe Abfrage zum Tagesende ergibt 1 246 Runs, davon 263 gescheitert, mit
gleichen Größenordnungen (31× `pysonar`, 12× Quality Gate). ADR 0034 nennt 2 244 Records; das
entspricht der Zahl der *Dateien* unter `runs/` (Run-JSON + Log, je eines pro Run, heute 2 493),
nicht der Runs.

## Entscheidung

1. **Fehlerklassen als Enum im Run-Modell** (ADR 0034):

   | Klasse | Bedeutung | Beispiele |
   |---|---|---|
   | `interrupted` | Run wurde von außen abgebrochen | Server-Neustart (Reaper), Dead-Man's-Switch |
   | `transient` | externe Abhängigkeit kurz nicht verfügbar | Netzfehler, HTTP 429/5xx, LLM-Rate-Limit (429 mit `Retry-After`), Usage-Fenster mit automatischem Reset, Backend `unhealthy`, Agent-Session ohne Antwort |
   | `environment` | Umgebung/Konfiguration kaputt; braucht einen Menschen | Exit 126/127, fehlendes Tool/File, ungültige Step-Konfiguration, HTTP 401/403, erschöpftes LLM-Quota/Billing (`insufficient_quota`, Usage-Cap ohne automatischen Reset) |
   | `outcome` | der Step hat ein echtes negatives Ergebnis | Tests rot, Quality Gate, Review `changes_requested`, Merge-Konflikt |
   | `unknown` | nicht klassifiziert | alles andere; Default |

   Die Menge ist bewusst klein. Neue Klassen nur per ADR-Änderung.

   **LLM-Limits unterscheiden sich danach, ob sie von selbst heilen**, nicht nach ihrem Namen:
   Rate-Limits (TPM/RPM, 429 mit `Retry-After`) und Usage-Fenster, die sich nach Zeitplan
   zurücksetzen (z. B. das rollierende Fenster eines Claude-Code-Abos), → `transient`.
   Erschöpfte Quotas/Billing-Limits ohne automatischen Reset (`insufficient_quota`,
   monatliche Caps, Zahlungsproblem) → `environment`: nur ein Mensch kann Limit oder Billing
   ändern, ein Backoff-Retry würde nur den Alert verzögern. Der Agent-Backend klassifiziert an
   der Quelle über den API-Fehlercode bzw. die strukturierte Limit-Meldung, nicht über den Text.

2. **Klassifiziert wird an der Quelle.** Steps melden Fehler typisiert: eine Exception-Hierarchie
   bzw. ein Step-Result mit `failure_kind`. Wer den Fehler erzeugt, weiß, was er bedeutet
   (Shell-Step kennt den Exit-Code, `gitops` den HTTP-Status, der Reaper den Abbruch).
   Kennt die Quelle bei `transient` den frühesten sinnvollen Wiederholungszeitpunkt
   (`Retry-After`, Reset-Zeit eines Limits), meldet sie ihn als `retry_after` (Zeitpunkt, UTC)
   mit; ohne diese Angabe bleibt das Feld leer. Der Run übernimmt `retry_after` zusammen mit der
   Klasse vom maßgeblichen Step (siehe unten). Die Engine
   übernimmt die Klasse des Steps, der den Run abgebrochen hat; brach keiner ab (nur Steps mit
   `gate: false` gescheitert), die des ersten gescheiterten Steps. Reaper und Dead-Man's-Switch
   setzen `interrupted`.

   **Agent-Step-Timeout** (die häufigste `transient`-Quelle, ADR 0012) klassifiziert der Step
   selbst, ohne Text-Parsing:
   - Timeout mit anhaltendem Backend-/Transport-Fehler (`· Backend-Fehler: …`) → `transient`.
   - Timeout, in dem die Session bis zuletzt ohne Fortschritt war (kein neuer Transcript-Eintrag
     im letzten Poll-Fenster), → `transient` (hängende Session).
   - Timeout, während der Agent noch arbeitete → `outcome` (Aufgabe passt nicht ins Zeitbudget;
     ein Wiederholen ändert daran nichts).

3. **Text-Klassifikation nur als eng begrenzter Fallback** für Ausgaben, die wir nicht
   kontrollieren (Agent-CLIs): genau ein Modul (`failures.py`), mit Tests je Muster. Kein
   Regex auf Fehlertexte anderswo.

4. **Default ist `unknown`, nicht geraten.** Ein Step-Typ, der seine Fehler noch nicht zuordnet,
   liefert `unknown`. Konsumenten müssen `unknown` als „nicht automatisch behandeln“ lesen.

5. **Die Klasse ist ein Vertrag für Konsumenten**, keine reine Anzeige:
   - UI zeigt die Klasse an Run-Liste, Historie und Run-Modal; Historie filterbar.
   - `run_failed`-Alerts tragen die Klasse; `environment` wird hervorgehoben, weil nur ein Mensch
     es lösen kann. Der Dedup-Fingerprint (ADR 0025) bleibt unverändert: Die Klasse folgt
     weitgehend aus Step und Fehlertext, die schon darin stecken.
   - Weitere Konsumenten (Retry nach ADR 0036, künftig z. B. spec-seitiges Routing nach Klasse)
     lesen ausschließlich `failure_kind`, nie den Fehlertext.

## Begründung

- **Eine Stelle entscheidet, viele reagieren.** Ohne Klassifikation würde jeder Konsument
  (UI, Alert, Retry) eigene Heuristiken auf Fehlertexte bauen.
- **An der Quelle ist es zuverlässig.** Texte ändern sich mit jeder Meldung, Exit-Codes und
  HTTP-Status nicht.
- **Allein schon nützlich und risikofrei:** Es ändert kein Ablaufverhalten, macht aber sichtbar,
  *warum* etwas scheitert. Der `pysonar`-Fehler wäre nach dem ersten Run aufgefallen.

## Abgelehnte Alternativen

- **Nur Regex auf Fehlertexte:** fragil, verstreut, bricht bei jeder geänderten Meldung.
- **Boolesches `retryable`** (wie in ADR 0012 für Agent-Sessions): beantwortet nur eine Frage.
  „Mensch alarmieren“ (`environment`) und „echtes Ergebnis“ (`outcome`) lassen sich damit nicht
  unterscheiden. `retryable` wird auf `failure_kind` abgebildet.
- **Klasse im Dedup-Fingerprint (ADR 0025):** kaum zusätzliche Trennschärfe, aber ein geänderter
  Fingerprint würde beim Umstieg für jeden offenen Alert ein neues Issue öffnen.
- **Feingranulare Taxonomie** (eigene Klassen für Git, HTTP, LLM, …): die Konsumenten brauchen
  die *Reaktion*, nicht die Quelle; die Quelle steht ohnehin im Fehlertext.

## Konsequenzen

- (+) UI und Alerts zeigen die Art des Fehlers; Umgebungsfehler eskalieren sofort erkennbar.
- (+) Grundlage für Retry (ADR 0036) und spätere spec-seitige Reaktionen, ohne Text-Parsing.
- (−) Jeder Step-Typ braucht eine bewusste Zuordnung seiner Fehler; bis dahin `unknown`.
- Altbestand ohne Feld wird als `unknown` gelesen: **kein Backfill** nötig. Das Run-Modell
  bzw. relationale Schema (ADR 0034, Entscheidungen 1 und 4) braucht für `failure_kind` und `retry_after` dennoch
  eine **Schema-Migration** für neu erzeugte Runs.
- `retry_after` wird beim Übernehmen validiert: nur bei `transient` zulässig (bei `environment`/
  `outcome` verworfen); ein Zeitpunkt in der Vergangenheit gilt als „sofort"; liegt er weiter als
  `max_wait` (ADR 0036, Entscheidung 1; Default `24h`, ab Ende des Runs gemessen) in der Zukunft,
  wird er darauf gekappt. Widersprüchliche
  Step-Werte löst das Maximum nach dieser Kappung.

## Offene Fragen

1. **Status `canceled` für menschlich abgebrochene Runs.** `interrupted` beschreibt heute nur
   systemseitige Abbrüche (Reaper, Dead-Man's-Switch). Ein vom Menschen initiiertes Cancel darf
   nicht als `interrupted` klassifiziert werden — ADR 0036 würde es sonst automatisch wieder
   starten. Zu klären, zusammen mit der Cancel-Funktion (die es heute nicht gibt): eigener
   Run-Status `canceled` im Zustandsautomaten (ADR 0034) mit den Konsequenzen kein
   `run_failed`-Alert, kein Auto-Retry, für reconcile (ADR 0014) als „hatte einen Run“ gezählt.
