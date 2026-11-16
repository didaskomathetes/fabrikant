# ADR 0001: Fabrik-Dogfooding — Fabrik entwickelt Fabrik

**Status:** Akzeptiert (2026-10-16)
**Entscheider:** Dominic
**Änderung:** 2026-10-17 — Modell-Zuordnung entkoppelt: die Invariante ist
Reviewer ≠ Builder; konkrete Modelle sind Deployment-Details (ändern sich schnell).
**Änderung:** 2026-10-19 — Modellwahl ist Konfiguration (**ADR 0022**); ersetzt
die Spalte „Modell“ und §3 Absatz 2.
**Änderung:** 2026-10-19 — `review-pr` auf den nativen `on_pr`-Trigger umgestellt
(§4): der Cron + Agent + State-Datei-Workaround ist entfernt; Dedup und
Re-Review-Triggering übernimmt die Engine.
**Kontext:** PR #10 (P0) und PR #11 (P1) sind gemerged. Die Fabrik ist produktionsreif genug,
sich selbst zu entwickeln. `kyudo-neandertal.fabrik.yaml` dient als Vorlage (Triage →
Human-Gate → Develop → Checks → PR).

## Entscheidung

Neues Projekt `projects/fabrik.fabrik.yaml` mit sechs Loops:

> Spalte „Modell“: Rollen, keine Bindungen — Modelle stehen in der Spec (ADR 0022).

| Loop | Trigger | Tier | Modell | Gate |
|---|---|---|---|---|
| `triage` | on_issue (`fabrik`) | T1 | (llm-Step) | vor `apply-label` |
| `develop` | on_issue (`ready to develop`) | T2 | Default (Builder) | vor `pr` |
| `refactor` | manual (`--var target=…`) | T2 | Default (Builder) | vor `pr` |
| `refactor-sweep` | manual | T2 | Default (Builder) | End-of-Run |
| `review-pr` | on_pr (`fabrik`) | T1 | Reviewer (≠ Builder, Leitprinzip 4) | — |
| `pr-reply` | cron `*/15` | T1 | Builder (wie `develop`) | — |

## Begründung der Details

### 1. Triage als T1 + Step-Approval statt T2
> **Ersetzt** (für den refine-Loop) durch ADR 0006 und für das Gate durch
> **ADR 0017**: Human-Gate nur bei `needs approval`/`close`.

T2 würde das Tier-Gate am **Ende** des Runs setzen — nach der Label-Anwendung. Das nützt
nichts. Daher: T1 (kein End-Gate) + `approval: true` am `apply-label`-Step → der Run pausiert
**vor** der Label-Änderung. Der Mensch sieht die Klassifizierung (Run-Record) und entscheidet.
Das ist genau "Issues refinieren nach Creation, dann Human Change".

### 2. Develop startet automatisch nach Triage
> **Ersetzt durch ADR 0017:** Das Refine-Gate ist nicht mehr das Human-Gate für
> `develop`; `develop` sichert sich über `human_gate` + PR-Review ab (ADR 0010).

`develop` triggert auf das Label `ready to develop`, das Triage anlegt (nach Approval).
Kein extra Human-Step nötig — das Triage-Gate ist das Human-Gate. Direktes Loslegen ohne
Human wäre `tier: 1` ohne Step-Approval; wir behalten T2 + PR-Approval, weil AGENTS.md
kritische Entscheidungen beim Menschen belässt. Labels sind self-healing: Triages
`ensure-labels`-Step legt `needs approval` + `ready to develop` an (`gh label create
--force`), beide sind zusätzlich vorangelegt im Repo. Ein develop-Run vor dem ersten
triage-Run ist ein stummer No-op (erwartet, kein Defekt).

### 2b. Git-Workflow: branch vorher, commit+push nach den Checks
Der Worktree liegt **detached** auf `origin/<branch>`. Daher: `git branch` (vor dem
Agenten) → Agent editiert → lint/test → `git commit` + `push` (`ship`-Step) → `pr`-Step
mit explizitem `head:`. Ein `action:`-Feld im git-Step ist Phantom (wird stumm ignoriert —
der Step kennt nur fetch/branch/commit/push), und ein detached HEAD würde
`head: HEAD` in die PR-Request erzeugen (GitHub lehnt ab). Commit `add: ["."]` respektiert
`.gitignore` (state/, runs/ landen nicht im Commit).

### 3. Cross-Model-Review (Leitprinzip 4: Builder ≠ Reviewer)
Die **invariante Regel**: Reviewer und Builder müssen unterschiedliche Modelle nutzen.
Der Reviewer hat frischen Kontext und eine andere Modell-Familie — Defekte, die beide
verpassen, sind die einzigen, die durchkommen. Konkrete Modell-Namen sind
Deployment-Details und ändern sich schnell mit dem Feld — sie stehen in der Spec
(`projects/fabrik.fabrik.yaml`), nicht hier.

> **Ersetzt durch ADR 0022:** Modell/Backend pro Step sind Spec-Konfiguration;
> `develop` und `pr-reply` teilen das Builder-Modell über YAML-Anker
> (`builder_model`), nicht über `FABRIK_AGENT_MODEL`.

`pr-reply` läuft bewusst mit dem **Builder-Modell** (wie `develop`, Env
`FABRIK_AGENT_MODEL`): der Replyer fixt Code auf dem PR-Branch und braucht daher
Builder-Kapazität. `review-pr` läuft mit einem günstigen, starken Modell via Copilot CLI
(konkretes Modell: Spec, YAML-Anker `reviewer_model`) — Review-Qualität bei kontrollierten Kosten.

### 4. `on_pr`-Trigger (früher: Cron + Agent + State-Datei)
**Ursprünglich (2026-10-16):** Ein `on_pr`-Trigger existierte nicht (→ Issue). Statt
Engine-Code zu schreiben, delegierten wir das Polling an den Agenten selbst: Cron startete
die Session, der Agent prüfte `gh pr list` gegen eine State-Datei
(`state/reviewed-prs.json`) und endete mit `nichts zu tun`, wenn nichts neues war.
Das war die Squid-konforme Version: kein neues Substrat, nur ein Prompt.

**Seit 2026-10-19:** Der native `on_pr`-Trigger existiert (GitHub-API-Polling wie
`on_issue`: `repo`, `labels`, `poll`, `states`). `review-pr` läuft jetzt auf
`on_pr` (Label `fabrik`): die Engine feuert bei neuen/aktualisierten PRs und setzt
`{pr_number}`/`{pr_title}`/`{pr_head_ref}`/`{pr_body}` als Variablen. Dedup
(keine PR doppelt) und Re-Review-Triggering (Update → neuer Run) übernimmt die
Engine — die State-Datei-Workaround-Logik ist aus dem Prompt entfernt.
(`pr-reply` nutzt weiterhin `state/replied-comments.json` — Kommentar-Dedup ist
eine andere Ebene als PR-Dedup und bleibt Agent-seitig.)

### 5. `gh`-CLI für Issue/PR-Operationen
Ein natives `issue`-Step existiert nicht (→ Issue). `gh` ist bereits im kyudo-Spec im Einsatz
und verfügbar. Bis der `issue`-Step da ist, bleibt `shell` + `gh` der Standardweg.
**Token-Hinweis:** Shell-Steps laufen in der fabrik-Prozess-Env (`fabrik.env` →
`GITHUB_TOKEN`). Agent-Steps laufen in der **opencode-serve**-Prozess-Env —
`GITHUB_TOKEN` muss beim Start von `serve` exportiert sein (Deployment-Voraussetzung,
siehe Folgen). Die Engine injiziert keine Env in Agent-Sessions (`create_task` hat kein
Env-Feld).

### 6. Base-Branch `fabrik/v0.3`
Aktuelle Entwicklungs-Hauptlinie (main steht noch bei v0.2). Wenn main die Trunk-Linie
wird, ändern wir `base:` in `develop`/`refactor`.

### 7. Refactoring als eigene Loops
`refactor` (gezielt, manuell mit `--var target=…`) + `refactor-sweep` (findet Code-Smells,
gießt Issues → füttert `refactor`). Boy-Scout-Regel aus AGENTS.md wird so systematisch:
kleine Verbesserungen inline, größere als Issues.

## Abgelehnte Alternativen
- **`on_pr`-Trigger sofort bauen:** P2-Mechanik (Intake-Queue), Overbuild für den
  Homelab-Beweis. Cron + Agent reicht.
- **Review-Step in der Engine (P1-Draft `review`-Step):** Wäre der saubere Weg, ist aber
  selbst ein unimplemented Feature (→ Issue). Das Prompt-basierte Review löst denselben
  Use Case ohne Engine-Änderung.
- **Ein Loop für alles:** Ein "super-Loop" (Triage + Dev + Review + Reply) wäre
  untestbar und unlesbar. Getrennte Loops = getrennte Runs, getrennte Audit-Trails.

## Folgen
- Neue Prompts: `prompts/fabrik-{develop,refactor,refactor-sweep,review,pr-reply}.prompt.md`
- Das Reviewer-Modell muss im Deployment verfügbar sein (Copilot CLI → `copilot` im PATH
  + `COPILOT_GITHUB_TOKEN`/`GH_TOKEN`/`GITHUB_TOKEN`). Copilot zieht Modelle regelmäßig
  zurück — bei `Model "…" is not available` das Modell in der Spec tauschen.
- `opencode serve` muss mit **`GITHUB_TOKEN` in der Env** gestartet werden (Agent-Steps
  rufen `gh` auf; Engine injiziert keine Session-Env).
- `state/` wird genutzt (bereits gitignored).
- Der dogfooding-Loop selbst ist Teil der Pipeline → seine Änderungen laufen durch die
  normalen Gates (T2/PR-Approval). Self-Improvement bleibt human-geprüft.
