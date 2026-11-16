# ADR 0013: Issue-Abschluss + Verifikation der Acceptance Criteria

**Status:** Akzeptiert (2026-11-04) — Stufen 0–2 umgesetzt, Stufe 3 bleibt
optional (nur bei nachgewiesenem Bedarf)
**Entscheider:** Dominic
**Kontext-Issue:** Vorschlag "QA-Agent nach Merge" (Chat 2026-10-19)

## Kontext

1. **Issues werden nie geschlossen.** Der `develop`-PR verweist nur mit
   „Issue #N“ auf das Issue, ohne GitHub-Closing-Keyword. Nach dem Merge bleibt
   das Issue offen, und niemand schließt es.
2. **Acceptance Criteria werden nirgends geprüft.** Der `refine`-Loop
   (ADR 0006) schreibt testbare AC ins Issue. Der `pr_review`-Step (ADR 0010)
   sieht aber nur den Diff und nicht das Issue. Ob der PR die AC erfüllt, prüft
   niemand.
3. **Vorschlag:** Ein separater QA-Agent soll nach dem Merge das Issue (v. a.
   die AC) gegen den neuen Code-Stand verifizieren. Der `refine`-Loop soll dafür
   einen QA-Vorschlags-Block liefern.

Kritische Prüfung des Vorschlags gegen die Leitprinzipien
(`framework-v1-draft.md`):

- **Prinzip 3 („Verification vor Review“):** Eine Prüfung erst nach dem Merge
  findet den Defekt, wenn er schon auf `main` liegt. Der Ausweg ist dann nur
  Reopen oder Revert. Die meisten AC lassen sich **vor** dem Merge prüfen.
- **Fehlerpfad:** Ein QA-Fail, der ein *neues* Issue anlegt, startet
  refine → develop → QA → neues Issue. Das ist eine unbegrenzte
  Feedback-Schleife, wie sie es mit Alert-Issues unter dem Label `fabrik`
  schon einmal gab.
- **§1.4 / ADR 0008:** Ein QA-Block ist LLM-Text, der aus einem untrusted
  Issue-Body abgeleitet ist. Ein Agent, der „QA-Vorschläge“ *ausführt*, ist
  eine Injection-Fläche.
- **ADR 0011:** Event-Trigger verpassen Zustand. Einen `on_merge`-Trigger gibt
  es nicht.
- **Prinzip 6 (Don't overbuild):** Ein eigener QA-Loop plus ein eigener
  QA-Block ist neue Infrastruktur, bevor die einfachen Hebel ausgeschöpft sind.

## Entscheidung

Gestuft. Jede Stufe nützt allein, die nächste kommt nur bei Bedarf.

### Stufe 0 — Closing-Keyword (umgesetzt)
Der Body des `develop`-PRs beginnt mit `Closes #{issue_number}`. GitHub schließt
das Issue beim (Squash-)Merge nach `main`.

**Regel „PR reicht zum Schließen“:** deterministisch, kein LLM-Urteil.
develop-PR ↔ Issue ist 1:1, also schließt der PR das Issue. Teil-PRs
(`Refs #N`) entstehen nur, wenn ein Mensch sie bewusst anlegt.

**Multi-Issue-PRs:** Die 1:1-Annahme gilt für die automatische
develop-Pipeline (ein Issue → ein PR → `Closes #N`). Ein PR, der mehrere
Issues betrifft (mehrfach `Closes #N` oder manuell angelegt), schließt alle
verlinkten Issues beim Merge — das ist beabsichtigt, aber außerhalb der
automatischen Pipeline. Für solche Fälle ist ein **expliziter menschlicher
Override** erforderlich: der Autor prüft, dass jedes verlinkte Issue
tatsächlich vollständig gelöst ist, bevor der PR gemerged wird. Die
automatische develop-Pipeline erzeugt solche PRs nicht.

### Stufe 1 — AC mit Prüfhinweis (statt separatem QA-Block)
`refine`/`classify` schreibt **jedes** AC als
`- [ ] <Kriterium> — Prüfung: <Test | Befehl | Beobachtung>`.

- Eine einzige Quelle der Wahrheit (AC). Ein separater QA-Block würde die AC
  duplizieren und davon wegdriften.
- Der Prüfhinweis ist ein **Hinweis** für Reviewer/QA, kein ausführbares
  Kommando. Deterministische Checks laufen nur als in der Spec definierte
  `shell`-Steps (`lint`, `test`, …), nie aus Issue-Text.
- Das entspricht der „Scenarios/Holdouts“-Idee aus `framework-v1-draft.md` §7.
  Echte Holdouts (vor dem Builder verborgen) sind im selben Repo nicht
  erreichbar, weil der develop-Agent das Issue lesen kann. Der Ausgleich ist
  Cross-Model: Prüfer ≠ Builder (Prinzip 4).

### Stufe 2 — AC-Verifikation im `pr_review` (vor dem Merge)
Kennt der Run eine `issue_number` (develop-Loop), lädt `pr_review` den
AC-Abschnitt des Issues und gibt ihn dem Reviewer **als untrusted Daten** mit.

- Der Verdict-Vertrag (ADR 0003) wird erweitert um
  `acceptance: [{criterion, status: met | unmet | unverifiable, evidence}]`.
- Mindestens ein `unmet` → `changes_requested` → die bounded Fix-Runden aus
  ADR 0010 greifen. Keine neue Schleife.
- `unverifiable` blockiert nicht, landet aber im Review-Text und im Run-Record.
- Der prompt-basierte `review-pr`-Loop (PRs außerhalb von develop) liest das
  verlinkte Issue (`Closes #N`) und prüft die AC ebenso, ohne Gate.

### Stufe 3 — Post-Merge-QA (nur bei nachgewiesenem Bedarf)
Optionaler `qa`-Step **im develop-Run nach `pr_merge`**. Damit braucht es keinen
neuen Loop und keinen `on_merge`-Trigger.

- Read-only Agent (kein Push, kein Schreib-Token), Prüfer ≠ Builder, prüft die
  AC gegen den aktuellen Stand von `main`. Liefert ein JSON-Verdict pro AC.
- Der Fehlerpfad ist deterministisch (§1.4 Regel 3): Ein `shell`-Step
  **reopent** das Issue mit den verfehlten AC als Kommentar und dem Label
  `qa-failed`. Es entsteht kein neues Issue, und develop startet nicht neu
  (`on_issue` feuert nur bei neuen Issues). Ein Mensch entscheidet.
- **Einführungs-Kriterium:** Stufe 3 kommt erst, wenn nach Stufe 2 Defekte
  nach dem Merge auffallen. Messbar als Defect Escape Rate (§5), z. B.
  Bug-Issues, die auf gemergte fabrik-PRs zeigen.

## Abgelehnte Alternativen

- **Separater QA-Loop jetzt:** Overbuild (Prinzip 6). Braucht einen
  `on_merge`-/Reconcile-Trigger und deckt mit Stufe 2 kaum mehr ab.
- **Separater QA-Vorschlags-Block im Issue:** Duplikat der AC, driftet. Wird
  durch den Prüfhinweis pro AC ersetzt.
- **QA schließt das Issue (statt `Closes #N`):** Das scheitert geschlossen,
  wenn der QA-Agent ausfällt: Issues bleiben dann für immer offen (vgl. den
  Copilot-Pipe-Bug vom 2026-10-19, der jeden Review-Run scheitern ließ). Mit
  `Closes #N` plus Reopen bei verfehlten AC bleibt das Issue-Schließen
  unabhängig von der Verfügbarkeit der Agents.
- **QA-Fail legt ein neues Issue an:** unbegrenzte Feedback-Schleife (s. o.).
- **LLM entscheidet, ob der PR das Issue vollständig löst:** nicht
  deterministisch, und 1:1 develop-PR ↔ Issue macht es überflüssig.

## Konsequenzen

+ Issues schließen sich mit dem Merge (Stufe 0, sofort).
+ AC werden vor dem Merge geprüft. Verfehlte AC lösen die bestehenden
  Fix-Runden aus (Stufe 2).
+ Kein neuer Loop und kein neuer Trigger. Prüfer ≠ Builder bleibt gewahrt.
- Stufe 1 ändert das `classify`-Schema und den Prompt des `refine`-Loops.
  Bestehende Issues haben AC ohne Prüfhinweis: Stufe 2 muss beide Formate
  lesen.
- Stufe 2 erweitert den Verdict-Vertrag aus ADR 0003 (Nachtrag dort nötig) und
  kostet einen größeren Review-Prompt.
- PRs außerhalb von develop (pr-reply, refactor, manuell) schließen Issues nur,
  wenn ihr Autor `Closes #N` setzt.
- Nachträge bei Annahme: ADR 0006 (AC-Format), ADR 0003/0010
  (Verdict-Vertrag, AC-Gate).
