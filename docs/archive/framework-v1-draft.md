> **Archived:** historical v1.1 design draft (2026-10-15). The framework has since evolved well beyond this draft; it is kept as a record of the original design discussion.
>
> **Superseded as a plan:** the still-valid content (principles, P0→P3 phases, KPI definitions) lives on in the living [roadmap](../roadmap.md), which reconciles this draft with what was actually shipped.

# Fabrik Framework — Entwurf v1.1 (General Purpose + Enterprise Scale)

**Stand:** 2026-10-15 · **Status:** DRAFT, nach kritischer Subagent-Review (v1.0) überarbeitet
**Ziel:** Fabrik von der Dev-Loop-Engine zur general-purpose Workflow-Fabrik:
dev-tasks **und** admin/research/ops-Workflows, Enterprise-Target (Cloud,
100e Agenten parallel), Homelab als Single-Node-Profil derselben Codebase.

**Changelog v1.0 → v1.1** (Review-Befunde eingearbeitet):
- `verify`- und `file`-Step aus P0 gestrichen (Overbuild, 1-File-Risiko) → P1
- `on_webhook` aus P0 gestrichen (127.0.0.1 ohne Auth + CORS `*` = Footgun) → P2
- Risk-Tier: P0-Semantik nur T1/T2; T3/T4-Semantik in P1/P2 (Substrat fehlt)
- Neue Sektion §1.4 „Untrusted Inputs" (Prompt-Injection war unbedacht)
- Phantom-Flag `gate` entfernt (existiert in v0.2 nicht; jeder Step stoppt
  per Default bei Failure); Status-Vokabular an Code angleichen
  (pass/fail/timeout/error/waiting)
- AgentBackend-Interface: `resume()`, `health()`, Budget, Cancel-Semantik,
  Event-Parität ergänzt (Interface wird NICHT vor SPIKE eingefroren)
- §3: Liveness (Heartbeat/Reaper), Cost-Caps, Rate-Limits/Admission-Control,
  Queue-Semantik (Priorität, Retry, DLQ), Idempotenz ergänzt
- Worktree-Wirklichkeit korrigiert: pro **Projekt**, Runs serialisiert —
  Sandbox-Isolation ist in P2 ein echter Sprung, keine Umbenennung
- P0-Sicherheit: Token-Masking in Logs, Spec-Hash im Run-Record,
  Stale-Run-Check, Self-Improvement-Loop immer durch T4-Gate
- Phasen-Grenzen explizit definiert (§6.1)

---

## Leitprinzipien (aus der 15.10-Research)
1. **Der Conductor ist Code, nicht ein LLM.** Deterministische Orchestration;
   LLMs nur als Worker in definierten Rollen.
2. **Session must survive** (Anthropic/Shopify): durable Event-Log;
   Harness und Sandbox sind Cattle, nicht Pets.
3. **Verification vor Review:** Checks + Verifikation laufen, bevor ein Mensch
   involviert ist. Mensch sitzt nur an irreversiblen/Accountability-Gates.
4. **Cross-Model-Review:** Builder ≠ Reviewer (idealerweise anderer Vendor).
   Defekt überlebt nur, wenn beide ihn verpassen.
5. **Trust wird in das System gebaut:** layered verification, observability,
   audit trail, accountability by design — **inkl. Trust-Grenzen für
   externe Inputs** (s. §1.4).
6. **Don't overbuild** (Squid-Lektion, O'Reilly): v1 der Fabrik war overbuilt
   → ungenutzt. Phasenweise, jede Phase muss allein nützen.
7. **Externer Input ist Daten, nicht Anweisung.** User-kontrollierter Text
   (Issues, Webhooks, gescrapte Seiten) ist die primäre
   Prompt-Injection-Oberfläche einer Workflow-Fabrik.

---

## 1. Kern-Abstraktionen

### 1.1 Workflow (ehem. "loop")
- Geordnete Steps. **P0: linear.** DAG/Fan-out (à la attractor) nur, wenn ein
  konkreter Use Case es verlangt — nicht vorab (Prinzip 6).
- Ein Workflow = deklarative Spec (YAML), versioniert im Repo.
- **Risiko-Tier** (`tier: 1|2|3|4`) als Datenfeld der Spec. Das
  Tier→Verhalten-Mapping lebt in **einer Funktion** (keine verstreute
  Code-Logik), damit P1/P2 es austauschen können.
  - **P0-Semantik (das gesamte Substrat, das P0 hat):**
    - T1 (Doku, Dep-Bumps, Reports) → Auto-Complete nach grünen Checks
    - T2+ (Features, Bugfixes, Admin-Aktionen) → Approval am Ende
  - **T3/T4-Semantik kommt erst, wo das Substrat existiert:**
    - T3 (Schema, Auth, API-Fläche) → + Spezial-Review/Cross-Model-Check (ab P1)
    - T4 (Architektur, Security-Policy, **Pipeline selbst**) → Human-Design,
      Fabrik nur vorbereitet. **Jede Selbständerung der Pipeline
      (Self-Improvement-Loop, §4) durchläuft immer das T4-Gate — auch bei
      T1-Workflows.** Kein Auto-Complete.

### 1.2 Step-Typen

Bestehende (v0.2): `shell`, `git`, `pr`, `agent` (CLI-Scaffold), `report`.

**P0 neu:**
| Typ | Zweck | ~Zeilen |
|---|---|---|
| `http` | Request senden, Response → Variable; Polling | ~70 |
| `llm` | Strukturierte LLM-Call: Prompt-Template + **Output-Schema** (hand-rolled Subset: parsebar + required-Keys + Typ-Checks, ~30 Zeilen; **keine** jsonschema-Dependency) | ~70 |
| `notify` | Benachrichtigung als Step (slack/webhook/email), reuse `_notify` | ~40 |
| `issue` | Native GitHub-Issue-Ops `create\|edit\|label\|close` (ADR 0018, Felder siehe README) | ~150 |

**P1 neu:**
| Typ | Zweck |
|---|---|
| `verify` | Liste von Checks (`type: shell\|http\|…`), alle müssen grün. **Nicht** "deterministisch-only" — sonst forkt P3 die Semantik (LLM-Judges, Screenshot-Diffs). In P0 ist ein Verifier einfach ein `shell`-Step (Exit-Code stoppt den Run per Default) — Konvention, kein neuer Typ. |
| `file` | Lesen/Schreiben/Template-Rendering (nur, wenn P0-Use Cases es wirklich brauchen) |

**Regeln:**
- Jeder Step kann **Outputs** emittieren → run-scope **Variable Store**,
  deterministische `{VARS}`-Substitution (existiert in v0.2).
  **Wichtig:** Substitutions-Regex aktuell `[a-zA-Z_]+` — erlaubt **keine
  Ziffern** (`{resp2}` wird stillschweigend nicht substituiert). P0:
  backward-kompatibel auf `[a-zA-Z0-9_]+` erweitern.
- **Kein `gate`-Flag** (v0.2-Phantom aus dem Chat-Helptext): In v0.2 stoppt
  **jeder** Step per Default den Run bei Failure. Das bleibt so. Ein
  non-blocking Step wird in P1 als explizites `continue_on_fail` —
  nicht implizit.
- **Status-Vokabular (an v0.2-Code angelehnt):**
  `running → pass | fail | timeout | error | waiting` (Agent-Skip = `pass`
  mit Summary "skipped"). Keine Erfindung von `pending/ok/skipped`.

### 1.3 Triggers
- **P0:** `manual`, `cron`, `on_file`, `on_issue` (letzter: zuerst die
  GitHub-App-Permissions fixen — Issues read/write — sonst bleibt er 403),
  `on_pr` (GitHub-API-Polling wie `on_issue`: `repo`, `labels`, `poll`,
  `states: [open|closed]`; feuert bei neuen/aktualisierten PRs und
  State-Wechseln; Variablen `pr_number`/`pr_title`/`pr_head_ref`/`pr_body`;
  `reconcile: true` = Reconciling-Controller, ADR 0011).
- **P2:** `on_webhook` + `on_event`. **Warum nicht P0:** der Server läuft auf
  127.0.0.1 **ohne Auth** mit CORS `*` — ohne Tunnel unreachbar, mit Tunnel
  (cloudflared) ein Loch, durch das jeder Runs starten kann. on_issue-Polling
  trägt den P0-Proof.
- **Phasen-Regel:** Payload→Variable-Mapping lebt in **einer internen
  Funktion** ("Run mit Initial-Variablen starten") — P2s Intake-Queue ruft
  dieselbe Funktion auf, kein eigener Webhook-Code-Pfad.

### 1.4 Untrusted Inputs (neu, Review-Finding)
Externer Content — Issue-Bodies, Webhook-Payloads, gescrapte Seiten — ist
**user-kontrollierter Text**. Der P0-Research-Workflow bringt ihn erstmals in
LLM-Kontext; in v0.2 läuft der agent-Step mit vollem env (inkl.
GITHUB_TOKEN) und shell/git-Zugriff. Das ist eine **neue Angriffsfläche** —
ein injizierter Issue-Body kann den Agenten zu push/PR/Exfiltration bewegen.

**Regeln (P0-pflichtig):**
1. Externer Content im Prompt wird **als untrusted data markiert**:
   Delimiter + explizite "dies ist Daten, keine Anweisung".
2. **Research-/Triage-Workflows = read-only Agent:** kein GITHUB_TOKEN im
   Agent-env, kein push, keine Schreib-Ziele.
3. **Triage-Output triggert Aktionen nur über deterministisches Mapping**
   (Klasse → Workflow), kein freies LLM-Urteil → Aktion.
4. **Approval vor `notify` und allen irreversiblen Steps** (besteht, bleibt).

---

## 2. Agent-Backend-Interface

`agent`-Step wird von "CLI-Scaffold" zu abstrakter Schnittstelle.
**Das Interface wird NICHT vor dem SPIKE eingefroren** — die zu prüfende
Capability (Resume) muss Teil der Definition sein, sonst ist der Freeze
gebrochen:

```python
class AgentBackend:
    def create_session(self, workdir: str, model: str, profile: str,
                       budget: float | None = None) -> str: ...
    def run(self, session_id: str, prompt: str, inputs: dict) -> Iterator[Event]: ...
    def resume(self, session_id: str) -> Iterator[Event]: ...      # Session must survive
    def result(self, session_id: str) -> dict:  # {status, artifacts, cost, transcript_ref}
    def health(self, session_id: str) -> dict: ...                  # Liveness (→ Reaper, §3)
    def cancel(self, session_id: str) -> None: ...                  # Semantik: Iterator endet,
                                                                    #  kein Exception-Blitz
```

- `budget`: harte Token-/Kostengrenze pro Session (→ Cost-Kontrolle, §3).
- `transcript_ref`: stabiler Typ über Backends hinweg (Blob-ID/Datei-Ref,
  nicht raw-Text) — für Audit.
- **Event-Parität ist ein Anspruch an das Interface, nicht an jedes
  Backend:** opencode = volle Fidelity (step_start/tool_call/message/cost);
  `cli`-Backend = reduzierte Fidelity (stdout → Event-Typ `log_line`,
  `cost: None` — **None, nicht 0**).

**Backends:**
1. **`opencode`** (primär): `opencode serve` → OpenAPI 3.1, headless,
   provider-agnostisch (75+ Provider inkl. lokal).
   **SPIKE PFLICHTIG (vor Interface-Freeze):** Session-Resume-Semantik,
   Cost-Reporting, Health/Liveness, Sandbox-/Workdir-Handling.
2. **`cli`** (bestehend, bleibt): beliebige Coding-Agent-CLI headless —
   Fallback, Homelab, exotische Agenten.
3. **`openhands`** (P3, optional): via SDK/ACP, falls sich ACP als
   Worker-Standard durchsetzt. Beobachten, nicht vorab adoptieren.
4. **`hermes`** (P3, optional): für nicht-dev Workflows, wo Hermes-Tools
   (Browser, Vault, Multi-Platform) Mehrwert haben.

**Modell-Routing:** Modell pro Step/Role — cost follows the difficulty:
Triage klein, Build stark, **Critique/Review: anderes Modell/Backend**
(Cold Critic, frischer Kontext). Reviewer kann den Run zurücksetzen,
Builder nicht.

**Phasen-Regel:** P0 führt **keine** neuen agent-Step-Spec-Keys ein —
P1s AgentBackend ändert die agent-Step-Spec (`backend`/`model`/`session`).
P0-Specs mit neuen Keys wären P1-Breakage.

---

## 3. Scale-Architektur (Enterprise, Cloud, 100e Agenten)

```
                        ┌────────────────────────────────────────────┐
                        │            CONTROL PLANE (stateless,       │
                        │            N Replikaten, skalierbar)       │
   Webhooks / Cron /    │  ┌────────────┐  ┌──────────────────────┐  │
   Issues / Events ────►│  │ Trigger +  │  │ Registry + Scheduler │  │
                        │  │ Intake     │  │ (Postgres: Workflows,│  │
                        │  └────────────┘  │  Runs, Variablen)    │  │
                        │                  └──────────┬───────────┘  │
                        │  ┌────────────┐  ┌──────────▼───────────┐  │
   UI / API / Chat ◄───►│  │ API + UI   │  │ Queue (Priorität,    │  │
                        │  └────────────┘  │ Max-Retry, DLQ) +     │  │
                        │  ┌────────────┐  │ Admission-Control     │  │
                        │  │ Approval-  │  │ (per-Repo/Provider-   │  │
                        │  │ Service    │  │ Caps, 429-aware)      │  │
                        │  │ (async,    │  └──────────┬───────────┘  │
                        │  │ SLA, Esk.) │  ┌──────────▼───────────┐  │
                        │  └────────────┘  │ Session-Log          │  │
                        │                  │ (append-only,        │  │
                        │  ┌────────────┐  │ Checkpoints) +       │  │
                        │  │ Budget-    │  │ Reaper (Heartbeats,  │  │
                        │  │ Service    │  │ Max-Job-Dauer) +     │  │
                        │  │ (Caps,     │  │ Budget-Tracking      │  │
                        │  │ Breaker)   │  └──────────────────────┘  │
                        │  └────────────┘                            │
                        └────────────────────────────────────────────┘
                                             │ Jobs
                        ┌────────────────────▼────────────────────────┐
                        │           WORKER PLANE (Cattle)             │
                        │  Worker ×N (Autoscale auf Queue-Tiefe,      │
                        │  MIT harter Worker-Cap)                     │
                        │  ┌────────────────────────────────────────┐ │
                        │  │ Sandbox (Docker/ECS/GKE-Pod,          │ │
                        │  │ PRO RUN, disposable): Repo-Checkout +  │ │
                        │  │ Agent-Backend (opencode serve) +       │ │
                        │  │ Heartbeat an Control Plane            │ │
                        │  └────────────────────────────────────────┘ │
                        │  Credentials: Vault auße... sandbox        │
                        │  (Git-Token im remote, MCP-Proxy)           │
                        └────────────────────────────────────────────┘
```

**Mechanismen (der Teil, den v1.0 nur als Diagramm stehen ließ):**

- **Durable Session-Log** (append-only, Postgres): jedes Step/Agent-Event,
  Cost, Artifacts-Ref, **Step-Checkpoints**. → Resume nach Worker-Tod,
  Replay, Audit, Metriken aus einem Log.
  ⚠️ Wirklichkeit von v0.2: Worktree ist pro **Projekt**, parallele Runs
  desselben Projekts werden per Lock **serialisiert**, nicht isoliert.
  "Per-Run-Sandbox" ist in P2 ein **echter Sprung** (eigenes Checkout
  pro Sandbox), keine Umbenennung.
- **Liveness / Dead-Man's-Switch:** Crash ≠ Hang. Hängen ist bei 100+
  Sandboxes der Regelfall (Agent im Loop, totgerocktes opencode-serve).
  → Worker sendet **Heartbeats** (via `AgentBackend.health()`), Control
  Plane kennt **Max-Job-Dauer pro Tier**, **Reaper** killt überfällige
  Jobs und queue't sie neu (unter Idempotenz-Bedingungen, s. u.).
  *(P0-Lightweight: ~20-Zeilen-Check "Run in Status `running` älter als
  X h → `error` + Alert" deckt den Homelab-Fall ab.)*
- **Cost-Kontrolle (nicht nur Metrik):** Budget als **First-Class-Feld**
  (pro Step/Run/Tag), harte **Worker-Cap**, **Kosten-Circuit-Breaker**
  (Fabrik-Pause bei > X/Tag), `budget` im AgentBackend-Interface.
  Runaway-Loop × 100 parallele Sandboxes ohne Caps = unbounded.
- **Admission-Control / Rate-Limits:** die echte Skalierungs-Grenze sind
  Provider-Limits (LLM RPM/TPM) und GitHub-Limits (~5000 API-Req/h,
  ~50 PR/h pro Repo). → Per-Repo- und per-Provider-**Konkurrenz-Caps**,
  Queue-Priorisierung, **429-aware Retry** mit Backoff. Intake-Triage
  ist nur die halbe Miete.
- **Queue-Semantik:** Priorität aus Risk-Tier (T4 ≠ T1 in derselben
  Queue), **Max-Retry-Count** + **Dead-Letter-Queue** — ein 3×
  fehlschlagender Job darf nicht endlos neu queue'd werden
  (Kosten-Explosion durch Design).
- **Idempotenz (die härteste P2-Aufgabe):** "Worker stirbt → Job neu
  queue'd" heißt: der Job **führt Steps erneut aus** — PR existiert schon,
  Notify ist raus, http-POST mit Side Effects. → **Step-Checkpoint-
  Granularität** im Session-Log, **`safe_to_rerun` als Step-Attribut in
  der Spec**, Idempotenz-Keys mindestens für `pr`/`notify`/`http`-POST.
- **Konkurrenz-Koordinierung:** Isolation verhindert, dass sich Runs
  sehen — nicht, dass sie sich das Mergen verbauen. → **Dedup-Regel**
  (1 Run pro Issue — Intake als Lock), per-Repo-Konkurrenz-Cap,
  **Branch-Namen mit Run-ID**.
- **Credential-Safety:** Vault außerhalb der Sandbox, Git-Token beim
  Provision ins Remote gebacken, MCP-Proxy für externe Services.
  **Non-Human-Identity** (Service Account) pro Run, Audit-Trail auf den
  Menschen, der den Workflow definiert hat.
- **Intake-Queue** als Fabrik-Eingabe: Issues/Alerts/Webhooks →
  `llm`-Triage (read-only, deterministisches Mapping, §1.4) → nur
  gelohnte Arbeit wird gestartet.

### 3.1 Homelab-Profil (dieselbe Codebase, anderes Deployment)
| Komponente | Enterprise | Homelab |
|---|---|---|
| Registry/Log | Postgres | SQLite |
| Queue | Message-Bus/SQS, DLQ | in-process |
| Sandbox | Docker/ECS/GKE (pro Run) | lokaler Worktree (pro Projekt, v0.2) |
| Worker | Pod-Pool ×N, Autoscale + Cap | 1 = die Maschine |
| Models | Cloud-Router + Budgets | Ollama |
| Liveness | Heartbeats + Reaper | Stale-Run-Check |

Kein Fork, kein Feature-Flag-Wildwuchs: ein **Deployment-Profil**
(`fabrik deploy --profile homelab|cloud`) wählt die Adapter.

---

## 4. Was von v0.2 übernommen wird (ist bereits 2026-konform)
- YAML-Spec als Single Source of Truth (Conductor = Code) ✓
- Checks + Approval ✓ (wird: tier-basiert, ab P2 async mit SLA)
- Worktree-Isolation pro Projekt ✓ (wird in P2: Sandbox-Isolation **pro Run**)
- Runs/History ✓ (wird: durable Session-Log)
- Alerts ✓ (wird: `notify`-Step + Incident-Roundtrip ab P3:
  Alert → Triage → Workflow → Hotfix-PR → Verify → Merge)
- LLM-Chat mit `apply` ✓ (wird: Self-Improvement-Loop — fehlgeschlagene
  Runs → Spec-Änderungs-Vorschlag → **T4-Gate** → apply; jeder
  Vorschlag zur Pipeline selbst ist Human-Approval, kein Auto-Complete)

**P0-Sicherheit (Review-Befunde):**
- **Token-Masking in Logs:** `shell`-Steps emittieren heute den
  **voll substituierten** cmd — Token in der Spec = Token im Run-Log.
  P0: env-Referenzen (z. B. in http-Headern) werden substituiert, im Log
  als `[REDACTED]` ausgegeben. Bestehendes cmd-Emit-Leak mit fixen.
- **Spec-Hash im Run-Record** (~5 Zeilen): git-sha oder Content-Hash der
  Spec — macht Replayability-KPI und Accountability ("wer hat welchen
  Workflow definiert?") nachweisbar.

## 5. Metriken (ab P1, Factory-KPIs)
- Cycle Time: Intent → gemergtes Artefakt
- Defect Escape Rate (was nach Merge auffällt)
- Cost per Change (Tokens + Review-Time + CI)
- Replayability Rate (Run reproduzierbar aus Inputs? — braucht Spec-Hash)
- Judge-Veto-Rate / Course-Correction-Rate (Spotify: ~25 % / ~50 %)
- **Hinzukommen (P2):** Queue-Tiefe, 429-Rate, Reaped-Jobs, Budget-Hits

## 6. Phasen (jede Phase muss allein nützen)

**Phase 0 — General Purpose in der bestehenden Engine** (Homelab, 1 File)
- Neue Step-Typen: `http`, `llm` (Subset-Schema-Validation), `notify`
  — **ohne** `verify`/`file` (P1)
- Variable Store + Substitutions-Regex auf `[a-zA-Z0-9_]+`
- Risk-Tier als Datenfeld, P0-Semantik (T1 = auto nach Checks, T2+ =
  Approval), Mapping in einer Funktion
- §1.4 Untrusted-Input-Regeln (read-only Research-Agent, Markierung,
  deterministisches Triage-Mapping)
- Stale-Run-Check (~20 Zeilen), Spec-Hash im Run-Record, Token-Masking
- **Kein** `on_webhook`, **kein** Queue-Konzept, **kein**
  State-Backend-Wechsel, **keine** neuen agent-Spec-Keys
- 2 Beispiel-Workflows als Proof: research (Issue/Topic → `llm`-Triage →
  `agent`-Recherche **read-only** → Report via shell → `notify` → Approval)
  und admin (z. B. Backup-Verifikation → Report → Alert)
- Volumen-Schätzung: ~+300 Zeilen → fabrik.py ~1650 Zeilen,
  Stdlib + PyYAML bleibt intakt
→ PR #10

**Phase 1 — Agent-Backends + Verifikation**
- SPIKE: opencode serve (Resume, Cost, Health, Workdir) — 1 Tag
- `AgentBackend`-Interface freeze (inkl. `resume`/`health`/`budget`) +
  `opencode`-Backend + `cli`-Backend (reduzierte Event-Fidelity)
- `review`-Step: Cross-Model Cold Critic (frischer Kontext, anderes Modell)
- `verify`-Step (Check-Liste-Semantik), `file`-Step (falls gebraucht)
- Session-Log in **SQLite — als das End-Schema definiert**
  (Postgres = SQLite + Server), P0-Run-JSON bleibt importierbarer Subset
→ PR #11

**Phase 2 — Control/Worker-Trennung + Enterprise-Mechanismen**
- Postgres-Registry + Queue (Priorität, Max-Retry, DLQ), stateless
  Control Plane
- Worker-Service + **Sandbox pro Run** (Docker), Checkpoints
- **Reaper** (Heartbeats, Max-Job-Dauer), **Budget-Service**
  (Caps, Breaker), **Admission-Control** (per-Repo/Provider-Caps, 429)
- **Idempotenz** (`safe_to_rerun`, Idempotenz-Keys), Koordinierung
  (Dedup, Branch-Namen mit Run-ID)
- Approval-Service mit SLA + Eskalation (implementiert das abstrakte
  Interface aus P1)
- `on_webhook` (mit Auth: shared-secret-Header, erreichbarer Endpunkt)
→ hier wird aus der Engine eine Architektur. PR #12+

**Phase 3 — Cloud-Deployment + Governance**
- ECS/GKE, Autoscale (mit harter Cap), Vault, Non-Human-Identities,
  Audit-Export
- Multi-Project/Multi-Team, Cost-Dashboards
- ACP-Backend (wenn Standard), Incident-Roundtrip
  (Sentry/CloudWatch → Triage → Hotfix-PR)

### 6.1 Phasen-Grenzen (was P0 NICHT darf, weil es P2 bricht)
1. Tier→Verhalten-Mapping als Datenfeld + eine Funktion — keine
   verstreute Logik; P1 friert das abstrakte Approval-Interface
   (request/decision/events) ein, das P2s Service implementiert.
2. Payload→Variable-Mapping in der einen internen Funktion
   ("Run mit Initial-Variablen starten") — P2s Intake ruft sie auf.
3. Keine neuen agent-Step-Spec-Keys (P1 ändert die agent-Spec).
4. Run-JSON bleibt importierbarer Subset des P1-Session-Log-Schemas —
   Schema wird in P1 definiert (als P2-End-Schema), nicht in P2.
5. Substitutions-Syntax nur backward-kompatibel erweitert.
6. Kein Queue-Konzept, kein State-Backend-Wechsel in P0/P1-Code.

## 7. Risiken & offene Fragen
- **Scope-Kollaps:** P2+ ist ein 6-12-Monate-Projekt. P0+P1 sind der
  Homelab-Beweis, dass die Patterns (Cross-Model-Review, read-only
  Research, Tiers) halten — erst dann Enterprise-Infra darunter.
- **P0-Risiko = Injection, P2-Risiko = Betriebsdynamik:** die
  Security-Frage (Untrusted Inputs) ist in P0 real, die
  Reliability-Fragen (Liveness, Cost, Idempotenz) in P2. Beides ist
  jetzt als Mechanismus (nicht als Metrik) im Entwurf.
- **opencode-Serve-Semantik unverified** → SPIKE vor Interface-Freeze.
- **Overbuild-Falle** (Squid): P0 bleibt ~+300 Zeilen; DAG/Parallelismus
  erst bei konkretem Use Case.
- **Scenarios/Holdouts** (StrongDM): "wenn Input X, dann muss Output Y
  enthalten" als `verify`-Check (ab P1).
- Lizenz: opencode MIT ✓, OpenHands MIT (core) ✓ — einbettbar.
