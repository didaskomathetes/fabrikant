> **Archived:** completed spike (2026-10-15). Current backend documentation lives in [`backends.md`](../backends.md); this file remains as the historical spike report (test results, pitfalls, API references).

# SPIKE: opencode serve als Agent-Backend (P1)

**Datum:** 2026-10-15
**Setup:** opencode 1.19.0 (`serve --port 8945 --hostname 127.0.0.1`), Ollama 0.33.2 (127.0.0.1:11434), `llama3.2:3b` (CPU-only, ~13,7 tok/s prompt-processing), Hermes-VM (kein GPU-Zugriff auf die 3090).
**Fazit:** ✅ **Tauglich als P1-Backend.** Vollständiger Agent-Loop (Planung → Tool-Call → Datei-Write) headless über HTTP-API. **P1-Integration verifiziert (2026-10-15):** `agent-smoke`-Loop grün (346 s, 9201 in / 43 out tokens, `smoke.txt` = "fabrik agent ok.", T2-Gate + Approval).

## Was funktioniert

| Test | Ergebnis |
|---|---|
| `POST /session` (model + permission rules) | ✅ 200, session-id |
| `POST /session/{id}/prompt_async` | ✅ 204, async |
| `GET /session/{id}/message` (polling) | ✅ vollständige Message-Liste mit `tokens`, `cost`, Parts |
| Agent-Loop: `create`-Tool-Call → Datei existiert | ✅ `hello.txt` = "spike ok." |
| Multi-Step-Session (2 Assistant-Messages) | ✅ step-start / tool / step-finish / text Parts |
| Token-/Cost-Accounting | ✅ input=9192, output=75, cost=0 (lokal) |
| `GET /api/health` | ✅ `{"healthy":true}` |

**Latenz (CPU, 3B):** erster Prompt ~3,5–9 Min (7038-Token-System-Prompt, 13,74 tok/s; mit Slot-Cache ~3,5 Min). Enterprise-Target: GPU/Cloud-Modelle → Sekunden.

## Critical Pitfalls (alle reproduziert)

1. **`chunkTimeout`** — opendes AI-SDK bricht Streams nach 5 Min ohne Daten ab (`DEFAULT_CHUNK_TIMEOUT=300_000`). CPU-Prompt-Processing streamt nichts → leere Assistant-Message, **kein Fehler-Event**. Fix: `provider.ollama.options.chunkTimeout: 3600000` (0 wird in 1.19.0 abgelehnt: "Expected a value greater than 0").
2. **Model-Liste leer** ohne explizite `limit`-Felder in der Config → `models.<id>.limit.{context,output}` setzen.
3. **`tools: true`** pro Model nötig, sonst keine Tool-Calls (lokale Modelle).
4. **Ollama-Cold-Start ~3,5 Min** (CPU) — Model vor Session starten warms halten (`keep_alive`).
5. **Ollama wedged-State:** llama-server-Prozess (mehrere Tage) blockierte plötzlich ALLE Requests (native + /v1), nur `systemctl restart ollama` half. → Health-Gate muss auf E2E-Request (nicht nur Port) prüfen.
6. **Slot-Cache-Bleed:** Ollama recycelt Slots über Konversationen (`sim > 0.100` → Cache-Hit) → Folge-Requests verarbeiten teils fremden Kontext. Für Isolation: `num_ctx` klein halten oder Session pro Task.
7. **`headerTimeout`-Default = 300 s (5 Min):** Ollama schickt die Response-Header erst mit dem ersten Token; bei CPU-Prompt-Processing kommt der erste Chunk oft nach >5 Min → opencode bricht ab (Ollama-Log: `500 | 5m0s | POST /v1/chat/completions`), leere Assistant-Message, **kein Fehler-Event**. `timeout` (gesamt) und `chunkTimeout` (zwischen Chunks) greifen hier nicht. Fix: `provider.ollama.options.headerTimeout: 1800000`.
8. **Interne Retries:** opencode retryt fehlgeschlagene LLM-Calls (maxRetries) — ein 5-Min-Abort kann 3×5 Min kosten, bevor die Session endgültig fehlschlägt.
9. **`finish: "stop"` ≠ Erfolg:** Das `finish`-Feld der Assistant-Message (`info.finish`) ist auch bei FAILED-Sessions `stop` (z. B. Ollama-500 → leere Message oder malformierter Tool-Call als `text`-Part, ohne `tool`-Part). Done-/Failed-Erkennung braucht daher: `finish` gesetzt (≠ `tool-calls`) **+ Substanz-Check** (erledigtes `tool`-Part ODER echter Final-Text; Tool-Call-JSON als Text zählt nicht). Zusätzlich: `info.error`-Feld, und Staleness-Check (`finish=None` ohne Parts seit >300 s → failed). Alle drei Fälle an realen Sessions verifiziert.
10. **Permission-Pending = Hang, kein Fehler:** Ein Tool-Call, der die `external_directory`-Permission triggert (Write außerhalb der Workdir), bleibt in headless serve **pending** (`tool.state.status: "running"`, `finish: null`, keine neue Message) — die Session endet weder done noch failed, sie hängt, bis der Step-Timeout greift. Reproduziert 2026-10-15: 3b-Modell hat den Write-Pfad halluziniert (`/tmp/smoke.txt` statt `/tmp/fabrik-agent-smoke/smoke.txt`) → Write blockiert (Datei entstand NICHT) → Session pendet. Sicherheitlich korrekt (Containment!), aber: bei schwachen Modellen Pfade im Prompt **absolut** halten, und der Step-Timeout ist der einzige Ausweg.

## Conductor-Integration (P1-Design)

- **Polling statt SSE** für die Basis (tick-orientiert, simpel, idempotent). SSE bleibt für Liveness-Monitoring.
- **Backend = Klasse mit Interface** (SPIE): `create_task(workdir, prompt, constraints) → session_id`, `poll(session_id) → running|done|failed`, `result(session_id) → {status, tokens, cost, output}`, `cancel(session_id)`.
- **Workdir = Fabrik-Worktree** (P0-Step) — Agent arbeitet im isolierten Worktree.
- **Security:** Agent-Step = T2 (schreibt Code, kein Deploy). Untrusted-Inputs (Issue-Bodies) gehen als **Daten** in den Prompt; der Conductor vertraut nur deterministischem Output (Status/Cost), nicht Agent-Prosa.
- **Readiness-Gate:** `GET /api/health` + E2E-Ping (wedge-Prüfung) vor Task-Dispatch.

## API-Referenz (opencode 1.19.0, 162 Endpunkte)

```
POST   /session                          → {id, ...}
POST   /session/{id}/prompt_async        → 204
GET    /session/{id}/message             → [{info: {role, tokens, cost, finish, error, time}, parts: [...]}]
POST   /session/{id}/abort               → 200
GET    /session/{id}/event               → SSE
GET    /api/health                       → {"healthy": true}
GET    /api/provider                     → Provider-Katalog
GET    /api/model                        → Model-Liste (nach Config)
```

Message-Parts: `step-start`, `text`, `tool` (name + parameters), `step-finish`.
Token-Structure: `{total, input, output, reasoning, cache: {read, write}}`.
`info.finish`: `null` (läuft) | `"tool-calls"` (Tool-Phase, nächste Message folgt) | `"stop"`/`"length"` (abschließend — **Success-Indikator nicht**); `info.time.created` (epoch-ms) für Staleness.

## Addendum: opencode 2.x (verifiziert 2026-10-17 gegen 2.0.32)

Die 2.x-API ist brechend anders (Details + Entscheidung: ADR 0005). Das
Backend unterstützt beide Versionen (auto-Erkennung). Kurzreferenz:

```
GET    /api/info                         → {"version": …}   (statt /api/health)
POST   /api/session                      → {"data": {id, …}} (location: {directory},
                                              permissions: [{action, resource, effect}])
POST   /api/session/{id}/prompt          → 200 ({"text": "…"} statt parts)
GET    /api/session/{id}/message         → {"data": […], "cursor": {next}} (paginiert)
POST   /api/session/{id}/interrupt       → 200 (statt /abort)
```

- **Auth Pflicht:** HTTP-Basic (`OPENCODE_SERVER_PASSWORD`; ohne Setzung
  generiert der Server ein Passwort, nur im Log) → `FABRIK_AGENT_PASSWORD`.
- **Message-Shape flach:** `type: "assistant"`, `content` (statt `parts`),
  top-level `tokens/cost/finish/error/time`; Tool-`name` ist ein Feld.
  Stabil geblieben: `finish`-Werte, Tool-`state.status`, `tokens`, `cost`.
- Workdir ist an die Session gebunden (`location`), kein `?directory=` mehr.

## Containment (verifiziert 2026-10-15)

- `edit`-Permission-Patterns sind **relativ zur Session-Workdir** — `*` deckt nur die Workdir ab.
- Pfade **außerhalb** der Workdir triggern die `external_directory`-Permission (Default `ask` → in headless serve = **pending** = Hang bis Step-Timeout; die Datei entsteht NICHT — verifiziert 2026-10-15, s. Pitfall 10).
- `bash "*` ist per Pattern **nicht** einzugrenzen (Shell kann alles) — echte Isolation = non-root User / Container / VM (Deployment-Voraussetzung).
