# ADR 0006: Issue-Refine-Loop (ersetzt Triage)

**Status:** Akzeptiert (2026-10-18); §„Human-Gate" **ersetzt durch ADR 0017** (2026-10-19);
Nachtrag 2026-11-04 (ADR 0013 Stufe 1): AC-Format mit Prüfhinweis
**Entscheider:** Dominic
**Kontext:** Der `triage`-Loop (ADR 0001) klassifizierte Issues nur (Label +
Acceptance Criteria bei `ready to develop`). Praxis-Feedback: Issues brauchen
vor der Entwicklung mehr Aufbereitung, und es gab keine saubere Abzweigung für
Duplikate/erledigte/nicht sinnvolle Issues. Zusätzlich Bug: `{issue_number}`
wurde im git-Step nicht substituiert (Branch `fabrik/auto/{issue_number}`
wörtlich angelegt) — gefixt im git- und pr-Step (Variable-Substitution für
`branch` bzw. `head`/`base`).

## Entscheidung

Der `triage`-Loop wird zum **`refine`-Loop** (`projects/fabrik.fabrik.yaml`):

| Step | Typ | Wirkung |
|---|---|---|
| `classify` | llm | Issue lesen → `label` + `reasoning` + `background` + `acceptance_criteria` + `close_reason` (JSON, untrusted-Markierung) |
| `ensure-labels` | shell | Labels `needs approval` + `ready for development` sicherstellen |
| `refine-body` | shell | `## Hintergrund (auto)` + `## Acceptance Criteria (auto)` an den Issue-Body anhängen (Idempotenz-Guard via `<!-- Refine (auto) -->`-Marker, mktemp + trap-Cleanup, 60s Timeout) |
| `apply-label` | shell, ~~**approval: true**~~ (ADR 0017: kein Gate) | Label anwenden (Human-Gate); bei `close` No-Op |
| `close-issue` | shell | Bei `close`: Begründung als Kommentar + `gh issue close` (nie delete) |
| `report` | report | Zusammenfassung in den Run-Record |

**Label-Semantik:**
- `ready for development` — klar umsetzbar, konsistent mit Docs/ADRs →
  `develop`-Loop triggert (Label-Umbenennung von `ready to develop`).
- `needs approval` — Architektur-Impact/Widerspruch zu ADRs → Issue wird
  trotzdem refinert; ein Mensch entscheidet und relabelt manuell.
- `close` — Duplikat (mit Referenz), bereits gelöst oder nicht sinnvoll →
  Issue wird **geschlossen mit Begründungs-Kommentar**, nicht gelöscht.

> **Ersetzt durch ADR 0017:** Gate nur noch bei `needs approval` (Approve →
> `ready for development`) und `close`; `ready for development` läuft ohne
> Pause durch. Der folgende Absatz ist historisch.

**Human-Gate:** `apply-label` hat `approval: true` — der Mensch schaut sich das
refinerte Issue an und gibt frei, bevor Label/Close wirken. Nichts wird
automatisch umgesetzt.

## Begründung
- **Refine statt Triage:** Triage lieferte nur ein Label; die eigentliche
  Arbeit (AC schreiben, Kontext herstellen) fehlte. Refine macht das Issue
  entwicklerfertig — der `develop`-Agent bekommt AC + Hintergrund im Issue.
- **Close als dritte Klasse:** Ohne Close-Pfad landeten Duplikate/Unsinn als
  `needs approval` und verstopften die Queue. Close mit Kommentar behält den
  Audit-Trail (kein Delete).
- **Hintergrund, nicht nur Fehler:** Feature Requests haben keinen Fehler —
  der Prompt verlangt Hintergrund zum Issue (Ursache/Kontext/Motivation).

## Abgelehnte Alternativen
- **LLM schreibt den kompletten Issue-Body:** Risiko, dass der LLM
  Original-Content verliert. Stattdessen: additive Sections (Hintergrund + AC)
  am Original-Body, idempotent.
- **Close ohne Human-Gate:** Duplikat-Erkennung kann falsch sein; Close ist
  (fast) irreversibel für den Reporter → bleibt hinter dem Approval-Gate.
- **Altes Label `ready to develop` behalten:** Umbenennung auf
  `ready for development` im selben Schritt, da der Loop ohnehin neu
  definiert wird (ein Label, ein Name).

## Folgen
- `develop`-Loop triggert auf `ready for development` (Spec angepasst).
- Bestehende Issues mit `ready to develop` müssen manuell umgelabelt werden
  (einmalig).
- git-Step substituiert `branch`, pr-Step `head`/`base` (Regression-Tests in
  `tests/test_steps_git_pr.py`); Spec-Tests in `tests/test_fabrik_spec.py`.
- ADR 0001 §1 (Triage als T1 + Step-Approval) wird für den `refine`-Loop
  durch diese ADR ersetzt; die übrigen Loops bleiben unverändert.

## Nachtrag 2026-11-04 (ADR 0013, Stufe 1)

`classify` schreibt **jedes** Acceptance Criterion im Format
`- [ ] <Kriterium> — Prüfung: <Test | Befehl | Beobachtung>` (Prompt +
Antwort-Schema). Der Prüfhinweis ist ein Hinweis für Reviewer/QA, **kein
ausführbares Kommando** — deterministische Checks laufen nur als in der
Spec definierte `shell`-Steps. Bestehende Issues ohne Prüfhinweis bleiben
lesbar: `pr_review` akzeptiert beide Formate (ADR 0013, Konsequenzen).
