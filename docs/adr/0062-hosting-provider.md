# ADR 0062: Hosting-Provider — Port/Adapter für Code-Hosting (GitHub, GitLab)

**Status:** Vorgeschlagen (2026-11-20)
**Betrifft:** ADR 0010 (Dual-Identity/Token), ADR 0011 (Approval-Kontext), ADR 0027 (Webhook-Trigger),
ADR 0031 (native Issue-Dependencies), ADR 0057 (Port/Adapter-Muster).

## Kontext

fabrik spricht Code-Hosting historisch **direkt über die GitHub-REST-API**: Steps und
Triggers rufen `gitops._gh_api` / `_gh_token` / `_gh_diff` / `_gh_dependencies` mit rohen
`api.github.com`-URLs auf (`pr`, `issue`, `pr_merge`, `human_gate`, `notify`, `scheduler`,
`retry_chains`, `apicommit`, `relay`). Das ist für GitHub korrekt, aber:

1. **Kein Self-Hosted-GitLab.** Ein self-hosted GitLab (z. B. `gitlab.opencode.de`) ist damit
   nicht als Host nutzbar — jede Stelle müsste manuell umgebaut werden.
2. **Provider-spezifisches JSON im Core.** Steps lesen GitHub-Felder (`head.sha`,
   `mergeable`, `labels[].name`, …) direkt aus dem API-Response. Das ist fragil und
   nicht portabel.
3. **Kein gemeinsamer Vertrag.** Es gibt keinen Ort, an dem die Operationen definiert sind,
   die Steps/Triggers von einem Host brauchen.

Das Port/Adapter-Muster (ADR 0057, `RunLauncher`; analog `AgentBackend`) ist im Codebase
etabliert und wird hier für das Hosting-Substrat übernommen.

## Entscheidung

1. **Port `Provider` (`fabrik/hosting/base.py`).** Der Core definiert den Vertrag als Union
   der Operationen, die Steps und Triggers brauchen: Issues (create/get/edit, Labels,
   Comments, List), PRs/MRs (create/get/list, merge, update-branch, files, diff, review,
   compare), native Issue-Dependencies, API-Commits (`commit_via: api`) und Webhook-Parsing.
   Strukturell (Duck-Typing) wie `AgentBackend`/`RunLauncher` — Adapter müssen nicht erben.
2. **Normalisierte Dataclasses als Vertrag.** `Issue`, `PullRequest`, `Comment`, `FileChange`,
   `CompareResult`, `WebhookEvent`, `RepoRef`. Ein Step sieht nie Provider-JSON, nur diese
   Typen. Jeder Typ hält das rohe Provider-Payload in `.raw` für die seltenen Felder ohne
   gemeinsamen Namen (z. B. `mergeable_state`).
3. **Adapter `GitHubProvider` / `GitLabProvider`.**
   - `GitHubProvider` ist ein dünner Wrapper um die bestehenden `gitops`-Helfer — der
     GitHub-Pfad bleibt byte-für-byte der bisherige (keine Verhaltensregression).
   - `GitLabProvider` implementiert den Vertrag über die GitLab-REST-API v4
     (`https://<host>/api/v4`, `PRIVATE-TOKEN`).
4. **Selektion + Credentials (`fabrik/hosting/select.py`).** `get_provider(repo, spec, role)`
   wählt den Adapter: explizites `hosting:`-Spec-Feld → Repo-URL-Host (`github.com` → GitHub,
   sonst GitLab) → `github:`-Feld → Default GitHub (rückwärtskompatibel). GitHub-Identity
   (Dual-Identity, ADR 0010) über `role`; GitLab-Token aus `FABRIK_GITLAB_TOKEN` (Env) oder
   `~/.config/gitlab/token` (Datei, `FABRIK_GITLAB_TOKEN_FILE`).
5. **Spec-Bindung pro Run.** Die Engine bindet das aktive Run-Spec per
   `hosting.spec_context(spec)` (ContextVar) für die Dauer eines Runs — Steps lösen den
   Provider daraus auf, ohne das Spec durch jeden Call zu reichen (analog
   `gitops.project_github`).

### Dokumentierte Lücken (GitLab)

- `update_branch`: kein GitLab-Äquivalent → `HostingNotSupported` (Ziel in Quelle per git mergen).
- `post_review`: `APPROVE` → Approve-Endpunkt; `REQUEST_CHANGES` → Note (kein eigener State).
- `merge_pr`: ignoriert `method` (squash/merge/rebase ist eine Branch-Einstellung, kein
  Merge-Parameter).
- native Issue-Dependencies: GitHub-only → `HostingNotSupported`.
- `edit_issue`: `state_reason`/`duplicate_issue_id` (GitHub-Close-Semantik) werden ignoriert.

## Konsequenzen

- (+) Self-hosted GitLab wird als Host nutzbar; `pr`, `issue`, `pr_merge` laufen über denselben
  Pfad auf GitHub und GitLab.
- (+) Steps sehen nur normalisierte Typen — Provider-Wechsel berührt keinen Step-Code.
- (+) GitHub-Pfad unverändert (Wrapper um `gitops`), bestehende Tests bleiben gültig.
- (−) Der Port ist die Union aller Operationen — er wächst, wenn Steps/Triggers mehr brauchen.
- (−) GitLab-Lücken (`update_branch`, Dependencies) sind echte Funktionalitätsunterschiede,
  keine Bugs; betroffene Features bleiben GitHub-only.
- (−) Migration der übrigen direkten `gitops`-Aufrufe (`human_gate`, `notify`, `scheduler`,
  `retry_chains`, `apicommit`, `relay`) auf den Provider ist schrittweise; bis dahin koexistieren
  beide Pfade.
