# ADR 0003: `review`-Step — Cross-Model Cold Critic

**Status:** Accepted (2026-10-17); Nachtrag 2026-11-04 (ADR 0013 Stufe 2):
Verdict-Vertrag um `acceptance` erweitert
**Issue:** #12

## Kontext

Der Review-on-PR-Use Case wurde in P0 prompt-basiert gelöst (ADR 0001,
`review-pr`-Loop mit qwen3:4b): ein Loop, der nach einem PR-Run manuell
oder per Scheduler angefasst wird. Das ist umständlich und lässt den
Review nicht in der Run-Semantik landen (kein Verdict im Run-Record,
kein Gate-Verhalten).

Ziel: ein Engine-Step, der **vor** dem PR-Step läuft und den Run bei
`changes_requested` sauber zurücksetzt.

## Entscheidung

**Neuer Step-Typ `review`:** startet eine Agent-Session mit **anderem
Modell** als der Builder-Step (frischer Kontext, kein Bias — derselbe
Modell-Typ zu reviewen, der den Code geschrieben hat, ist Selbstlob).

- Step-Def: `type: review`, `model` (Pflicht), `backend`/`endpoint` wie
  agent, optional `target` (Default: Output des letzten Agent-Steps) und
  `prompt_file` (ersetzt die Review-Vorlage).
- **Cross-Model-Check:** Die Engine trackt das Modell des letzten
  Agent-Steps (`ctx["last_agent_model"]`). Review mit gleichem Modell →
  Step failt. (Engine-Check, nicht nur Doku-Versprechen.)
- **Verdict-Vertrag:** Der Reviewer antwortet mit striktem JSON
  (`verdict: approved | changes_requested`, `findings[]`, `summary`).
  Parsing ist tolerant (erstes `{` bis letztes `}`), unlesbarer Output →
  Step failt (kein Silent-Pass).
- **Run-Semantik:** Verdict + Findings landen im Run-Record
  (`step.review`). `changes_requested` → Step fail → Run fail
  (gate: true, Findings in der Step-Summary). Der Reviewer kann also den
  Run zurücksetzen, der Builder nicht.
- Reuse: `_step_agent_backend` aus dem agent-Step (Session-Lifecycle,
  Orphan-Schutz, Usage-Recording, Session-Log-Events) — kein zweiter
  Backend-Pfad.

## Konsequenzen

+ Verdict im Run-Record → querybar (Session-Log), exportierbar
  (Tracing-Exporter), gate-fähig
+ Review im selben Run wie Build (deterministische Abarbeitungsreihenfolge)
+ Cold Critic erzbar, nicht nur empfohlen
- Review-Model muss erreichbar sein (zweites Backend/Modell im Homelab)
- Reviewer-Output ist LLM-Text: Verdict-JSON-Vertrag ist eine
  Vereinbarung, kein Protokoll — daher failt unparseable Output statt
  zu raten

## Alternativen (abgelehnt)

- **Review als zweiter agent-Step + prompt:** kein Cross-Model-Check,
  kein Verdict im Record, kein Run-Reset — genau der P0-Umweg, den dies
  ersetzt.
- **Review im PR-Step (vor dem Push):** PR-Step bleibt simpel; Review
  vor PR ist die saubere Reihenfolge und testbar ohne Git-Infra.

## Nachtrag 2026-11-04 (ADR 0013, Stufe 2)

Der Verdict-Vertrag wird um das optionale Feld `acceptance` erweitert:
`[{criterion, status: met | unmet | unverifiable, evidence}]` — ein Eintrag
pro Acceptance Criterion des verlinkten Issues (gesetzt durch `pr_review`,
das dem Reviewer die AC als untrusted Daten mitgibt). Fehlendes Feld →
leere Liste. Ungültige Struktur (kein List, Eintrag kein Dict, leeres
Kriterium, unbekannter Status) wird **abgelehnt** (kein Verdict → Step
failt), nicht still ignoriert. `acceptance` landet im Run-Record
(`step.review`) — auch `unverifiable`, das nicht blockiert.
