# ADR 0010: Dual-Identity PR-Pipeline (Builder ≠ Reviewer, Mensch nur bei kritisch)

**Status:** Accepted (2026-10-18); Amendment 2026-10-27 (PR #364): Konkrete
Identitäten sind Konfiguration, keine Architektur — die Invariante "zwei
verschiedene Identitäten" ist die Entscheidung.
Amendment 2026-11-03 (PR #453): Identitäten pro Projekt überschreibbar
(`github_env_file`); optional von GitHub signierte Commits (`commit_via: api`).
Nachtrag 2026-11-04 (ADR 0013 Stufe 2): AC-Verifikation im `pr_review`
(strukturiertes `acceptance`-Verdict, `unmet` → `changes_requested`).

## Kontext

Ziel: PR-Pipeline ohne Menschen im Loop — außer bei kritischen Entscheidungen
(ADR-Änderung, große Refactoring/Architektur-Changes, Datenverlust, GDPR, …).

**Kernproblem: Review-Prozesse müssen von einer anderen Identity kommen als
die PR-Autorin.** Ein PR kann **nicht** von derselben Identity approved
werden, die ihn erstellt hat (User ODER App — GitHub-Regel, Self-Approval
wird abgelehnt). Dasselbe gilt für Review-Events allgemein (APPROVE /
REQUEST_CHANGES, Merge-Freigabe): sie müssen von einer Identity kommen, die
separat von der autorisierenden Identity ist. Vollautomatische Approval
braucht also zwingend **zwei verschiedene Identitäten**.

## Entscheidung

**Identitäts-Split: Builder und Reviewer sind immer zwei verschiedene
Identitäten.**

- Die **Builder-Identity** (Developer) arbeitet im Worktree, pusht die
  Branch, legt den PR an und **mergt** (squash) nach Approval.
- Die **Reviewer-Identity** (QA) reviewt den PR (APPROVE /
  REQUEST_CHANGES) — sichtbar als eigene Identity, nie die des Builders.
- **Die zwei Identitäten müssen verschieden sein** — das ist die
  Architekturentscheidung. Review-Prozesse (Approval, Changes-Request,
  Merge-Freigabe) klappen nur, wenn sie von einer Identity kommen, die
  separat von der PR-Autorin ist; eine Identity, die eigene PRs approvet,
  wird vom Host abgelehnt.

**Konkrete Identitäten sind Konfiguration, keine Architektur** (wie
Modellwahl, ADR 0022): Welche Identitäten Builder und Reviewer konkret
sind — GitHub-Apps, PATs, User, auf einem anderen Host (z. B. GitLab)
andere Bot-Identitäten — bestimmt die Projekt-Config (Spec/`fabrik.env`),
nicht diese ADR. Ein anderes Projekt auf GitLab nutzt andere Identitäten;
das Prinzip (zwei verschiedene Identitäten, Review separat von der
Autorin) bleibt gleich.

*Historische Bindungen (Konfiguration, nicht Teil der Entscheidung):*
- 2026-10-18: agent-host-App (Builder) + persönlicher PAT (Reviewer).
- 2026-10-27 (PR #364): zwei GitHub-Apps — `fabrik-builder` (Builder) +
  `fabrik-reviewer` (Reviewer), beide mit 1h-Installation-Tokens.

**Beispiel: Token-Handling (GitHub-Implementierung, Config):**
Konkretes Beispiel, wie die zwei Identitäten in der Projekt-Config
abgebildet werden:
- `GITHUB_TOKEN` = Builder-Installation-Token (Developer). Wird extern per
  `scripts/gh-token-refresh.py` (Cron) alle ~30 min erneuert und in
  `FABRIK_ENV_FILE` (Default `/root/.hermes/.env`) geschrieben — die Datei
  wird pro API-Call neu gelesen, kein Restart nötig.
- `GITHUB_REVIEWER_TOKEN` = Reviewer-Installation-Token (QA). Ebenfalls 1h
  gültig, per `scripts/gh-token-refresh.py --role reviewer` (Cron) erneuert.
- `gitops._gh_token(role)` mit `role="dev" | "reviewer"`.

**Neue Steps:**
- `pr_review`: LLM-Review des PR-Diffs (Cross-Model, ADR 0003-Vertrag) +
  GitHub-Review via Reviewer-Token (`APPROVE` / `REQUEST_CHANGES`).
  `changes_requested` → bounded Fix-Runden (Developer-Agent fixt im Worktree,
  pusht, Re-Review; Default `max_rounds: 2`). Danach immer noch
  `changes_requested` → Step fail.
- `pr_merge`: squash-merge via Developer-Token nach Approval.
- `human_gate`: Kritizitäts-Check am PR-Diff. Heuristiken (ADR-Pfade,
  Diff-Größe, Keywords) **und** LLM-Meinung. Kritisch → Run pausiert
  (`needs_approval`), Mensch approvet via `fabrik approve`. Nicht kritisch →
  auto weiter. Statisch "immer menschlich" bleibt `approval: true` in der
  Loop-Spec (bestehende Semantik).

**Engine:** Steps können dynamisch pausieren — Step-Result
`{"needs_approval": true, "approval": {...}}` → Run geht in
`needs_approval` (analog zum statischen `approval: true`, aber zur Laufzeit
entschieden). Resume führt den Step erneut aus; der Step merkt sich den
Zustand per ctx-Variable (`human_gate_passed`), die über
`record["vars"]` persistiert wird.

## Beispiel-Loop (vollautomatisch, Mensch nur bei kritisch)

```yaml
loops:
  auto-dev:
    description: Issue → PR → Review → Merge (Mensch nur bei kritisch)
    tier: 1  # T1: kein synthetisches Tier-Gate am Ende
    triggers:
      - on_issue
    steps:
      - name: develop
        type: agent
        prompt: "Löse das Issue {issue_title}: {issue_body}"
      - name: push
        type: git
        branch: "fabrik/auto/{issue_number}"
        commit: {message: "fabrik: {issue_title}"}
        push: true
      - name: open-pr
        type: pr
        repo: owner/name
        title: "fabrik: {issue_title} (#{issue_number})"
      - name: review
        type: pr_review
        repo: owner/name
        model: <anderes Modell als develop>
        max_rounds: 2
      - name: gate
        type: human_gate
        repo: owner/name
      - name: merge
        type: pr_merge
        repo: owner/name
        method: squash
```

**on_pr-Trigger-Hinweis:** Ein `REQUEST_CHANGES`-Review ändert `updated_at`
des PRs → ein `on_pr`-Trigger feuert. Fix-Runden laufen bewusst INNERHALB
des `pr_review`-Steps (bounded), nicht über on_pr-Re-Runs — on_pr-Loops
sollten per `labels` gefiltert werden, um Review-Updates nicht als
neue Work-Trigger zu interpretieren.

## Amendment 2026-11-03 (PR #453): Identitäten pro Projekt, signierte Commits

Die Invariante (Builder ≠ Reviewer) bleibt. Zwei Erweiterungen, beide opt-in:

- **`github_env_file: <abs. Pfad>` (Projekt-Spec):** Token-Datei nur für dieses
  Projekt (andere App-Installation/Org, PAT als Reviewer). Runs, Trigger-Polls,
  Retry-Versuche und alle Subprozesse (Shell, Agent-`gh`, Relay-CLI) lesen
  `GITHUB_TOKEN`/`GITHUB_REVIEWER_TOKEN` dann **nur** aus dieser Datei; globale
  Token-Env-Werte gelten dort nicht (sie gehören zu einer anderen Identität).
  Ohne die Option bleibt alles wie oben (Env → `FABRIK_ENV_FILE`).
- **`commit_via: git|api` (Projekt-Spec, Default `git`):** Repos mit
  `required_signatures` lehnen die lokal erzeugten, unsignierten Bot-Commits ab
  (Squash-Merge hilft nicht). Mit `api` baut fabrik jeden fehlenden Commit über
  die Git-Data-API nach, GitHub signiert ihn für die Builder-App. Trade-off:
  Autor/Zeitstempel der lokalen Commits gehen verloren (Autor = App-Bot), keine
  Root-Commits. Abgesichert durch Tree-SHA-Vergleich pro Commit und
  non-fast-forward-Prüfung wie bei `git push`.

Abgelehnt: ein Signing-Key auf dem Host (Schlüsselverwaltung, widerspricht der
App-Identity) und ein globaler Token pro Prozess (kann nicht mehrere
Installationen bedienen).

## Nachtrag 2026-11-04 (ADR 0013, Stufe 2): AC-Verifikation im `pr_review`

`pr_review` verifiziert die Acceptance Criteria des verlinkten Issues: der
AC-Abschnitt geht dem Reviewer als **untrusted Daten** ins Prompt, der Verdict
trägt pro Kriterium `acceptance: [{criterion, status: met|unmet|unverifiable,
evidence}]` (Verdict-Vertrag, ADR 0003-Nachtrag) und landet im Run-Record
(`step.review`).

- **≥ 1 `unmet` → `changes_requested`** — deterministisch in der Engine
  (auch gegen ein `approved`-Verdict des Reviewers); jedes unmet-Kriterium
  wird als high-Severity-Finding ergänzt, damit die bounded Fix-Runden
  (`max_rounds`) es adressieren. Keine neue Schleife.
- **`unverifiable` blockiert nicht**, ist aber im GitHub-Review-Text und im
  Run-Record nachvollziehbar.
- Ungültiges `acceptance`-JSON wird abgelehnt (kein Verdict → Step fail),
  nicht still ignoriert.

## Konsequenzen

+ Vollautomatischer Kreislauf: PR → Review → Approval → Merge, ohne Mensch
+ Mensch nur bei kritisch (Heuristik + LLM, deterministisch nachvollziehbar
  im Run-Record: `step.approval` mit Gründen)
+ Approval-Identity = Mensch → GitHub-Branch-Protection ("require 1 review")
  wird zur harten Merge-Gate, die GitHub selbst erzwingt
+ App kann eigene PRs nicht approvieren → Self-Approval strukturell unmöglich
- Zweites Token (`GITHUB_REVIEWER_TOKEN`) in der Umgebung; Reviewer-App
  braucht Contents:read + Pull requests:read/write
- Builder-App braucht Contents:read/write + Pull requests:read/write + Issues:read/write
- Fix-Runden sind LLM-Work: kosten Tokens, bounded via `max_rounds`
- `human_gate`-LLM-Call pro PR: ein zusätzlicher Call (aktivierter Provider)

## Alternativen (abgelehnt)

- **PAT erstellt, App approvet:** funktioniert, aber Machine-PRs tragen die
  persönliche Identity als Author (schlechtere Audit-Story).
- **Review nur lokal (bestehender `review`-Step), kein GitHub-Review:**
  keine sichtbare PR-Approval → Branch-Protection als Merge-Gate nicht
  nutzbar, kein Audit-Spur in GitHub.
- **Unbounded Fix-Runden:** Risiko Endlosschleife (Reviewer und Builder
  im Ping-Pong) → bounded + Run-Fail als Ausweg.
- **Kritizität nur statisch (`approval: true`):** verpasst unerwartet
  kritische Changes (z. B. Agent "vergisst", eine ADR anzufassen).
