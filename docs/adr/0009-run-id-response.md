# ADR 0009: `run_id` vor Worker-Start + `resumed`-Flag in der API

**Status:** Draft (2026-10-19) — erfordert Freigabe (PR #76)

## Kontext

Die Web-UI (`web/app.js`) startete einen Run per `POST /api/runs` und pollte
danach die Run-Historie (`GET /api/runs?project=…`), um den neuen Run zu
finden. Das war ein **Start-Race**: Der Worker-Thread schreibt den Run-Record
asynchron; kurz nach dem POST ist der neue Run noch nicht in der Historie
sichtbar → die UI pollte gegen den leeren/alten Stand.

Analoges Problem am Approval-Gate: `POST /api/approve`/`/api/reject` lieferten
nur einen Reply-String. Die UI wusste nicht, ob tatsächlich ein Resume-Worker
gestartet wurde (z. B. schlägt fehl, wenn das Projekt-Lock belegt ist) → sie
konnte nicht entscheiden, ob sie den Run weiter verfolgen soll.

## Entscheidung

Zwei **additive** API-Erweiterungen (keine Breaking-Change):

1. **`run_id` vor Worker-Start:** `POST /api/runs` erzeugt die `run_id`
   (`config.now_id()`) **bevor** der Worker-Thread startet und liefert sie im
   Response zurück. Die UI pollt danach gezielt per `GET /api/run/<id>`
   (`pollRunById`) — kein Suchen in der Historie, kein Start-Race.
2. **`resumed`-Flag:** `POST /api/approve`/`/api/reject` liefern zusätzlich
   `resumed` (bool). `true` nur, wenn für den Run tatsächlich ein
   Resume-Worker gestartet wurde; `false`, wenn der Run bereits läuft,
   abgeschlossen ist oder das Projekt-Lock belegt ist. Die UI verfolgt den
   Run nur, wenn `resumed` true ist.
3. **Interne Contract:** `_chat_resume()` (chat.py) liefert das Tupel
   `(reply, resumed)`; der Server leitet `resumed` an die UI weiter.

## Begründung

- **Warum `run_id` vor dem Start?** Die ID ist deterministisch erzeugbar
  (`config.now_id()`), ohne den Run auszuführen. Sie als Handle zurückzugeben
  macht den Run sofort adressierbar — das Race verschwindet strukturell,
  statt per Polling-Heuristik umgangen zu werden.
- **Warum `resumed` als bool und nicht nur Reply-Text?** Ein maschinenlesbares
  Flag ist deterministisch auswertbar; ein Reply-String wäre Parsing-Heuristik
  in der UI.
- **Warum additive statt Breaking?** Bestehende Clients, die nur `reply`
  lesen, bleiben kompatibel (additives JSON-Feld). Abgedeckt durch
  `test_post_approve_legacy_client`.

## Konsequenzen

- UI (`web/app.js`) nutzt `pollRunById` und `r.resumed` — defensive
  (`if (r && r.resumed)`), damit ältere Clients ohne das Feld nicht brechen.
- `run_id`-Uniqueness hängt an `config.now_id()` (Zeitstempel-basiert);
  Kollisionen unter hoher Parallelität sind unwahrscheinlich, aber nicht
  ausgeschlossen — bei Bedarf stärkere ID-Generierung (Follow-up).
- README-API-Tabelle + `chat.py`-Modul-Docstring dokumentieren das neue
  Contract für Integratoren.
