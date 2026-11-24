# ADR 0039: Konvergente PR-Review-Runden und Fixes durch den Builder

**Status:** Vorgeschlagen (2026-10-23)
**Ergänzt:** ADR 0010 (Dual-Identity-PR-Pipeline, Fix-Runden), ADR 0003 (Verdict-Vertrag).

## Kontext

Runs `20261023T000806Z-…` (PR #311) und `20261023T001958Z-…` (PR #313) scheiterten
mit „nach 3 Runden immer noch changes_requested“, obwohl jede Fix-Runde die
gemeldeten Findings behoben hatte. Ursachen:

- Jede Review-Runde ist ein frischer Reviewer ohne Kenntnis der Vorrunde. Er
  sucht von vorn und findet auf einem großen Diff immer etwas Neues — kein
  einziges Finding wurde zweimal gemeldet.
- Die Fix-Runden liefen mit Backend/Modell des **Reviewers** (`step.backend`/
  `step.model`), entgegen ADR 0010 („Developer-Agent fixt“). Der Cold Critic
  korrigierte seine eigenen Findings, der nächste Cold Critic desselben Modells
  bewertete das.

## Entscheidung

1. **Vorrunden-Kontext:** Ab Runde 2 enthält der Review-Prompt die Findings der
   Vorrunde. Der Reviewer verifiziert sie zuerst; ein neues Finding meldet er
   nur, wenn es `high` ist oder eine Regression der Fixes. Sind alle behoben und
   gibt es kein solches neues Finding, ist das Verdict `approved`.
2. **Finding-Limit:** `max_findings` (Default 5) — höchstens so viele Findings pro
   Runde, schwerste zuerst, keine Nitpicks. Die Engine sortiert nach Severity und
   kappt zusätzlich, falls der Reviewer das Limit ignoriert.
3. **Suggestions:** Ein Finding darf ein optionales Feld `suggestion` (kurze,
   konkrete Änderung) tragen. Es landet im GitHub-Review-Body und im Fix-Prompt
   als Hinweis — der Reviewer schreibt selbst keinen Code.
4. **Fixes durch den Builder:** Die Fix-Runde nutzt Backend/Modell des letzten
   Agent-Steps (`ctx.last_agent_backend`/`last_agent_model`, von der Engine
   getrackt), überschreibbar per `fix_backend`/`fix_model`. Ohne Builder-Step
   greifen `FABRIK_AGENT_BACKEND`/`FABRIK_AGENT_MODEL`.

## Konsequenzen

+ Review-Runden konvergieren: die Ziellinie bewegt sich nicht mehr pro Runde
+ Fix-Runden in der Qualität des Builders; Cross-Model-Trennung bleibt intakt
+ Kürzere, priorisierte Reviews (weniger Tokens, weniger Rauschen auf dem PR)
- Ein in Runde 1 übersehenes Medium-Finding wird ab Runde 2 nicht mehr gemeldet
  (bewusst: Runde 1 ist der volle Review; `high` bleibt immer blockierend)
- Das Limit kann bei sehr großen Diffs echte Findings abschneiden — große
  Änderungen gehören ohnehin in mehrere Issues (siehe Offene Punkte)

## Offene Punkte (nicht Teil dieser Entscheidung)

- **Severity-Gate** (`block_on: [high]`): Medium/Low in der letzten Runde →
  Approve + Follow-up-Issue statt Run-Fail.
- **Issue-Splitting im `refine`** bei großem Scope (Backend + Frontend + ADR)
  über Sub-Issues mit Blocker-Abhängigkeiten (ADR 0031) statt gestackter PRs.
