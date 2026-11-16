# ADR 0034: Typisiertes Run-Modell + relationales Storage (SQLite / Postgres)

**Status:** Akzeptiert (2026-10-21)
**Umsetzung:** #274 (Schritte #275–#279, Folgen #280, #281)
**Betrifft:** ersetzt ADR 0002 in der Rolle „Run-JSON = Source of Truth, Session-Log =
Projektion“. Berührt ADR 0015, 0019, 0028, 0030 und PR #215 (Run-Historie).

## Kontext

Ein Run ist ein untypisiertes `dict`, das Engine, Scheduler, Server, Chat, CLI und Session-Log je
nach eigener Vorstellung lesen und schreiben. In den 2 244 Records unter `runs/` sind 9 Felder
immer vorhanden, 11 nur manchmal; Steps tragen je nach Typ weitere Felder. `status` ist ein freier
String ohne definierte Übergänge, Unterzustände (`approval_queued`, `waiting_step ==
"tier-approval"`) sind implizit. Records werden komplett überschrieben (last writer wins),
Approve/Reject prüfen und schreiben ohne Atomarität, Abfragen lesen alle Dateien. Run-JSON und
`sessionlog.db` halten dieselben Daten doppelt.

Ein anderes Speichermedium allein (etwa JSON-Spalten in einer DB) löst das nicht.

## Entscheidung

1. **Runs sind typisiert.** Run, Step, Approval, Usage und Review-Ergebnis werden ein Domänenmodell
   aus Dataclasses mit Enums für alle Status-Werte, unabhängig von der Persistenz. Kein Code
   außerhalb des Stores arbeitet mehr mit Run-`dict`s.
2. **Zustandsautomat.** Erlaubte Übergänge sind einmal definiert und werden erzwungen:
   Run `running → needs_approval → running → pass | fail | error`, `needs_approval → rejected`,
   Reaper `running | needs_approval → error`. Approval `pending → (queued →) approved | rejected`;
   `approval_queued` und der Tier-Gate werden damit echte Zustände.
3. **Übergänge statt Record-Writes.** Geschrieben wird über Operationen eines `RunStore`
   (Run/Step starten und beenden, Approval anfordern und entscheiden, Reap). Jede ist eine
   Transaktion mit Vorbedingung; ein unerlaubter oder veralteter Übergang schlägt fehl.
4. **Strikt relationales Schema, keine JSON-Spalten im Kern.** Tabellen für Runs, Steps,
   Review-Ergebnisse, Approvals, Run-Variablen (Key/Value mit Typ) und Events, mit Constraints
   für Status, Pflichtfelder und Fremdschlüssel. Einzige Ausnahme: eine als opak dokumentierte
   Textspalte für fremde Payloads an Systemgrenzen (z. B. Tool-Calls der Agent-Backends).
5. **Suche direkt auf den Run-Tabellen** mit normalen, indizierten Abfragen; kein eigener
   Suchindex. Der Log-Text bleibt vorerst nicht durchsuchbar. Ein Index kommt erst bei gemessenem
   Bedarf, mit eigenem ADR.
6. **Technik:** SQLAlchemy Core + Alembic, kein ORM. SQLite im Homelab, Postgres in der Cloud,
   umgeschaltet über eine DB-URL.
7. **Eine Quelle der Wahrheit:** Die DB ersetzt Run-JSON und `sessionlog.db`. Run-Logs, Secrets,
   Specs und die Agent-State-Dateien im Worktree bleiben Dateien.
8. **Migration:** einmaliger Import der bestehenden Runs mit Dry-Run-Report; JSON wird archiviert,
   kein Dual-Write.

**Reihenfolge** (je ein PR): (1) Modell + Zustandsautomat, Persistenz noch JSON; (2) Engine, Chat,
CLI, Scheduler auf Modell und Store-Operationen umstellen; (3) SQL-Store (SQLite) inkl. Session-Log
und Suche; (4) Import und Umschalten; (5) Postgres.

## Nicht Teil dieses ADR

- Mehrere Instanzen gegen eine DB (Run-Slots, Scheduler, Worktrees sind hostlokal) → eigener ADR.
- Übriger State (`state`-Step, Pause, Branch-Overrides, Benutzer) → folgt in denselben Store; die
  Spec-Option `file:` des `state`-Steps braucht eine eigene Entscheidung.

## Konsequenzen

- (+) Ungültige Runs sind weder im Code noch in der DB darstellbar; nebenläufige Übergänge sind atomar.
- (+) Indizierte Abfragen statt Datei-Scans, ein Store statt zwei.
- (−) Jedes neue Feld braucht eine Migration (gewollt).
- (−) Neue Abhängigkeiten (SQLAlchemy, Alembic, psycopg); Runs nicht mehr per `cat`/`jq` lesbar → CLI-Export.
- (−) Schritt 2 berührt große Teile der Engine; Aufwand ca. 2–3 Wochen.
- (−) Records mit unbekanntem Status sind im Modell nicht darstellbar und daher aus den
  Suchergebnissen ausgeschlossen (nur über `load_report` sichtbar); bewusste
  Verhaltensänderung seit Schritt 1 — in den PR-Text von #277 (SQL-Store) aufnehmen.

## Offene Fragen

1. Steps beim Start als `running` persistieren (neu) statt erst am Ende?
2. Run-Variablen: alle skalaren Variablen oder nur die für Resume nötigen?
3. Postgres-Tests in jeder CI oder nur nightly?
