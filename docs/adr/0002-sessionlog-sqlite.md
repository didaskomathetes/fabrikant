# ADR 0002: Durable Session-Log in SQLite (End-Schema)

**Status:** Accepted (2026-10-17)
**Teilweise ersetzt durch:** ADR 0034 (2026-10-21) — Run-JSON ist nicht mehr Source of Truth, das Session-Log keine Projektion mehr; beides geht im relationalen Run-Store auf. Das Event-Schema bleibt die Grundlage.
**Issue:** #14

## Kontext

P0 speichert Runs als JSON-Dateien (`runs/<projekt>/<id>.json`). Das ist stateless
und für den Homelab-Betrieb richtig — aber es gibt keine querybare Historie,
keine lückenlose Event-Reihenfolge und keine Agent-Telemetrie (Tool-Calls,
Kosten pro Session). Der Framework-Draft definiert die Phasen-Grenze:
"Run-JSON bleibt importierbarer Subset des P1-Session-Log-Schemas — Schema wird
in P1 definiert (als P2-End-Schema)."

Ziel: ein SQLite-Session-Log, das in P1 das **End-Schema** trägt. P2 tauscht dann
nur das Storage-Backend (Postgres = SQLite + Server), nicht die Semantik.

## Entscheidung

**Ein SQLite-File als append-only Event-Store** neben den Run-JSONs. Die
Run-JSON bleibt Single Source of Truth für die Engine (stateless Worker,
Resume-Logik unangetastet). Das Session-Log ist eine **durable, querybare
Projektion** — kein zweiter Storage mit eigener Semantik (gleiche
Architektur-Logik wie der Tracing-Exporter: Rohdaten exportieren, nicht forken).

### Schema (v1)

```sql
CREATE TABLE sessions (
    session_id   TEXT PRIMARY KEY,          -- = Run-ID
    project      TEXT NOT NULL,
    loop         TEXT NOT NULL,
    trigger      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,             -- running|pass|fail|error|needs_approval
    spec_hash    TEXT NOT NULL DEFAULT '',
    started_at   TEXT NOT NULL,             -- ISO-8601 UTC
    finished_at  TEXT,
    tokens_in    INTEGER NOT NULL DEFAULT 0,
    tokens_out   INTEGER NOT NULL DEFAULT 0,
    cost_usd     REAL NOT NULL DEFAULT 0,   -- Rohdaten vom Agent (keine Preistabelle!)
    source       TEXT NOT NULL DEFAULT 'native'  -- native | import
);

CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    seq         INTEGER NOT NULL,           -- Reihenfolge innerhalb der Session
    ts          TEXT NOT NULL,              -- ISO-8601 UTC
    kind        TEXT NOT NULL,              -- run_start|run_end|step_start|step_done|
                                            -- tool_call|message|cost|approval
    step_name   TEXT NOT NULL DEFAULT '',
    step_type   TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT '',
    data        TEXT NOT NULL DEFAULT '{}'  -- JSON, redacted
);
CREATE INDEX idx_events_session ON events(session_id, seq);

CREATE TABLE artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    step_name   TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,              -- report | prompt | output
    path        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE checkpoints (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    step_idx    INTEGER NOT NULL,
    step_name   TEXT NOT NULL,
    status      TEXT NOT NULL,              -- nur 'pass' wird gecheckpointet
    data        TEXT NOT NULL DEFAULT '{}', -- JSON: duration, usage, summary
    PRIMARY KEY (session_id, step_idx)
);
```

### Event-Kinds

| kind | Quelle | data |
|------|--------|------|
| `run_start` | Engine bei Run-Anfang | `{"loop", "trigger"}` |
| `run_end` | Engine bei Run-Ende | `{status}` |
| `step_start` | Engine vor Step-Dispatch | `{type}` |
| `step_done` | Engine nach Step | `{status, duration_s, usage?}` |
| `tool_call` | Agent-Step pro neuem Tool | `{tool}` |
| `message` | Agent-Step bei Session-Ende | `{text}` (gecappt) |
| `cost` | Agent-Step bei Session-Ende | `{tokens_in, tokens_out, cost_usd}` |
| `approval` | Engine bei Gate-Pause | `{step}` |

### Invarianten

1. **Append-only:** events/checkpoints/artifacts werden nie aktualisiert, nur
   eingefügt. `sessions` wird per `UPDATE` finalisiert (status, finished_at,
   Token-/Kosten-Summen) — die einzige mutable Zeile pro Session.
2. **Redaction am Write-Pfad:** alle `data`-Blobs und `message`-Texte laufen
   durch `secrets._redact`, bevor sie persistiert werden.
3. **Rohdaten, keine Kalkulation:** `cost_usd` stammt vom Agent-Backend
   (opencode `cost`-Feld). Keine Preistabelle in fabrik — ein Exporter rechnet
   Preise aus den Rohdaten (Tokens + Model + Provider).
4. **P0-Run-JSONs bleiben importierbar:** `fabrik import-runs` liest
   `runs/*.json` und projiziert sie (sessions + step_done-events +
   checkpoints, `source='import'`, Totals aus Step-Usage backgefillt).
   Import ist idempotent: bestehende Sessions werden nicht überschrieben
   (native Runs gewinnen). Best-effort: Connect-Fehler → 0, defekte
   Dateien werden übersprungen, Commit pro Datei.
5. **Keine Engine-Abhängigkeit:** Das Log ist best-effort — ein Log-Fehler
   (z. B. defekte DB) fällt einen Run nicht. Die Run-JSON bleibt der
   autoritative Zustand.

### Storage

- Pfade: `FABRIK_HOME/sessionlog.db` (Override: `FABRIK_SESSIONLOG`)
- WAL-Mode (parallele Lese während Runs schreiben)
- Ein globaler Connection + Lock (Homelab-Skalierung: wenige Runs, viel I/O
  wäre überdimensioniert). P2: Connection-Pool, dann Postgres.

## Konsequenzen

+ Querybare Historie (SQL) über alle Runs, inkl. P0-Runs via Importer
+ Agent-Telemetrie (Tool-Calls, Kosten) ohne Backend-Änderung am Polling
+ Checkpoints als P2-Basis für Worker-Resume (stateless Worker zieht den
  letzten Checkpoint, nicht die ganze JSON)
+ Schema ist Postgres-kompatibel (keine SQLite-spezifischen Typen) → P2
  Storage-Swap ohne Schema-Migration
- Zweites Persistenz-File neben den Run-JSONs (bewusste Redundanz:
  Projektion vs. Source of Truth)
- WAL-File (`sessionlog.db-wal/-shm`) in `FABRIK_HOME` — Backup-Skripte
  sollten das DB-File per `sqlite3 .backup` sichern, nicht per `cp`

## Alternativen (abgelehnt)

- **Events direkt in die Run-JSON:** keine sequenzielle Querybarkeit,
  Datei-Größe explodiert bei Agent-Runs (Hunderte Tool-Calls), Resume-Pfad
  wird komplizierter.
- **Postgres von Anfang an:** P2-Infrastruktur (Server) für ein P1-Feature.
  SQLite ist der Endpfad minus Server — der Draft-Entscheid.
- **OTel-Collector als Log:** ein Daemon pro Homelab-Machine für Telemetrie,
  die ohnehin aus den Run-Daten projizierbar ist. Exporter (OTel/Langfuse)
  kommen später **dazu**, sie ersetzen dieses Log nicht.
