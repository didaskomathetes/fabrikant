# ADR-Index

Architektur-Entscheidungen (Architecture Decision Records) für fabrikant.

**Konvention:** nächste freie Nummer über `main` **und alle offenen PRs**
(`python scripts/check-adr-numbers.py next`; Kollision reparieren: `… fix`), Dateiname `NNNN-kurz-titel.md`,
Status-Header (`Vorgeschlagen` / `Akzeptiert` / `In Überarbeitung` / `Draft` / `Abgelehnt`).
ADRs ändern nur mit expliziter Freigabe (AGENTS.md) — Drafts laufen als
gestapelter ADR-PR (ADR 0011). Eindeutige Nummern und ein vollständiger Index
werden in CI geprüft (`tests/test_adr_index.py`), Kollisionen blockieren den Merge
(ADR 0032). Lücken werden nie neu vergeben: 0007 war auf `main` vergeben (→ 0011), 0016 gehörte
zu PR #251 (ungemergt geschlossen), 0033 zu PR #255 (dort zugunsten einer Änderung an ADR 0017
zurückgezogen), 0029 zu PR #215 (Run-Suchindex; entfällt, Suche direkt auf den Run-Records).

**Umnummerierung 2026-10-20:** parallele Branches hatten Nummern doppelt vergeben.
Die zuerst gemergte ADR behielt ihre Nummer; die übrigen wurden in Merge-Reihenfolge
verschoben: 0013-review-verdict-marker → 0024, 0014-alert-dedup-fingerprint → 0025,
0018-alert-log-tail-redaction → 0026, 0023-on-webhook-trigger → 0027,
0023-worktree-per-run → 0028.

| # | Titel | Status |
|---|-------|--------|
| [0001](0001-dogfooding-loops.md) | Fabrik dogfooding — Fabrik develops Fabrik | Accepted |
| [0002](0002-sessionlog-sqlite.md) | Durable session log in SQLite (end schema) | Accepted, partially superseded by 0034 |
| [0003](0003-review-step.md) | `review` step — cross-model cold critic | Accepted |
| [0004](0004-branch-switching.md) | Branch switching at runtime (UI) | Accepted, UI part removed (2026-11-04) |
| [0005](0005-opencode-dual-version.md) | opencode backend — dual support 1.x/2.x | Accepted |
| [0006](0006-issue-refine-loop.md) | Issue refine loop (replaces triage) | Accepted |
| [0008](0008-shell-untrusted-input-env.md) | Shell steps with untrusted input — `env:` instead of inline substitution | Accepted (amended 2026-11-03: trust default `extra_context`) |
| [0009](0009-run-id-response.md) | `run_id` before worker start + `resumed` flag in the API | Draft |
| [0010](0010-dual-identity-pr-pipeline.md) | Dual-identity PR pipeline (builder ≠ reviewer, human only when critical) | Accepted; amendment 2026-10-27: concrete identities = config; amendment 2026-11-03: per-project identities, signed API commits |
| [0011](0011-approval-request-protocol.md) | Approval request protocol (ADR PR stacked on the original PR) | Accepted |
| [0012](0012-agent-transient-backend-errors.md) | Agent step resilient to transient backend errors | Accepted |
| [0013](0013-issue-closing-ac-verification.md) | Issue-Abschluss + Verifikation der Acceptance Criteria | Akzeptiert (Stufen 0–2; Stufe 3 optional) |
| [0014](0014-on-issue-reconcile.md) | Reconcile-Modus für den `on_issue`-Trigger | Akzeptiert; Amendment 2026-10-28: Auto-Retry via ADR 0036 |
| [0015](0015-queued-approval.md) | Freigaben bei belegtem Projekt-Lock einreihen | Akzeptiert |
| [0017](0017-refine-conditional-gate.md) | Refine-Loop — Human-Gate nur bei `needs approval` / `close` | Akzeptiert |
| [0018](0018-issue-step.md) | Nativer `issue`-Step | Akzeptiert |
| [0019](0019-pause-resume-disable.md) | Pause/Resume/Disable pro Projekt und Loop | Akzeptiert (geändert 2026-10-23) |
| [0020](0020-worktree-free-loops.md) | Worktree-freie Loops mit eigenem Lock und Scratch-Workdir | Akzeptiert |
| [0021](0021-issue-priority-labels.md) | Issue-Priorisierung über Phasen-Labels (`priority_labels`) | Akzeptiert |
| [0022](0022-model-choice-is-config.md) | Modellwahl ist Konfiguration, keine Architektur | Akzeptiert |
| [0023](0023-on-pr-reconcile-per-head.md) | `on_pr` reconcile pro Head-SHA (`reconcile: head`) | Akzeptiert |
| [0024](0024-review-verdict-marker.md) | Review als GitHub-Review mit Verdikt-Marker, Inline-Threads, Reply-Sperre | Akzeptiert |
| [0025](0025-alert-dedup-fingerprint.md) | Alert-Deduplizierung per Fingerprint (statt Issue pro Run) | Akzeptiert |
| [0026](0026-alert-log-tail-redaction.md) | Redaction des Alert-`log_tail` | Akzeptiert |
| [0027](0027-on-webhook-trigger.md) | `on_webhook`-Trigger (HMAC, Tunnel-CORS) | Akzeptiert |
| [0028](0028-worktree-per-run.md) | Worktree pro Run statt Projekt-Lock (`max_parallel`, geteilter Agent-State) | Akzeptiert (geändert 2026-10-23) |
| [0030](0030-auth-local-users-roles.md) | Authentifizierung + Autorisierung (lokale Benutzer, Rollen) | Akzeptiert |
| [0031](0031-issue-duplicates-dependencies.md) | Duplikat-/Abhängigkeits-Erkennung im Refine, fehlende Voraussetzungen als Issue, Blocker-Gate im Develop (`skip_blocked`), Backfill | Akzeptiert |
| [0032](0032-adr-number-check.md) | ADR-Nummern — Vergabe, Kollisionsprüfung, Umnummerierung | Akzeptiert |
| [0034](0034-run-domain-model-relational-store.md) | Typisiertes Run-Modell + relationales Storage (SQLite / Postgres) | Akzeptiert |
| [0035](0035-failure-classification.md) | Fehlerklassifikation gescheiterter Steps und Runs (`FailureKind`) | Akzeptiert; Amendment `max_wait`-Kappung (Draft) |
| [0036](0036-run-retry.md) | Run-Retry mit Backoff und Ablage erschöpfter Aufträge | Akzeptiert (inkl. Amendment `max_wait`, Trigger-Schlüssel, Claim) |
| [0037](0037-relay-tasks.md) | Relay-Tasks — langlaufende Aufgaben als Kette kurzer Agent-Sessions (Issue = Auftrag, Branch = Zustand, blindes Audit) | Draft |
| [0038](0038-run-live-stream-sse.md) | Live-Log (SSE) und Agent-Transkript im Run-Modal | Vorgeschlagen |
| [0039](0039-pr-review-convergence.md) | Konvergente PR-Review-Runden (Vorrunden-Kontext, Finding-Limit, Suggestions) und Fixes durch den Builder | Vorgeschlagen |
| [0040](0040-on-slack-trigger.md) | `on_slack`-Trigger (Polling, Bot-Token, Whitelist) | Akzeptiert |
| [0043](0043-reconcile-dedup-queue-and-paused.md) | Reconcile-Dedup — eingereihte Trigger und pausierte Runs | Vorgeschlagen |
| [0044](0044-acp-backend-observation.md) | ACP-Backend (OpenHands via SDK/ACP) — beobachten, nicht adoptieren | Akzeptiert (2026-11-13) |
| [0045](0045-incident-roundtrip.md) | Incident-Roundtrip (Sentry/CloudWatch → Triage → Hotfix-PR) | Akzeptiert (2026-11-13) |
| [0046](0046-oidc-login.md) | OIDC-Login (generischer Client, optionales Extra `fabrikant[oidc]`) | Akzeptiert (2026-11-13) |
| [0047](0047-spec-sources-vars-templates.md) | Spec-Quellen (`FABRIKANT_PROJECTS_PATH`, Zielrepo), spec-relative Pfade, `vars`, Loop-Templates | Akzeptiert, Nachtrag 2026-11-03 (Worktrees außerhalb des Checkouts) vorgeschlagen |
| [0049](0049-ui-i18n.md) | UI-Sprache — Message-Kataloge de/en, Locale-Formatierung | Akzeptiert |
| [0050](0050-agent-env-allowlist.md) | Agent-Env-Allowlist — Agent-Prozesse erben kein `os.environ` | Vorgeschlagen |
| [0051](0051-egress-default-deny-proxy.md) | Egress default-deny — Agent-Internetzugang nur über den Allowlist-Proxy | Akzeptiert |
| [0052](0052-named-connections.md) | Benannte Verbindungen — LLM-Provider und Agent-Backends in der UI konfigurieren | Akzeptiert (2026-11-06) |
| [0053](0053-sonar-pr-project-cleanup.md) | Sonar-PR-Projekte nach Merge/Close aufräumen (Python-Helfer, fail-safe, advisory im main-Scan) | Akzeptiert (2026-11-13) |
| [0054](0054-per-run-agent-sandbox.md) | Per-Run-Agent-Sandbox — One-Shot-CLI-Agenten in einem Docker-Container | In Überarbeitung (2026-11-06; amended 2026-11-25: shell steps in the sandbox) |
| [0055](0055-ui-delegated-event-handlers.md) | UI ohne Inline-Event-Handler (data-* + Delegation) | Akzeptiert (2026-11-13) |
| [0056](0056-deployment-profile-adapter-registry.md) | Deployment-Profil (`homelab\|cloud`) mit Adapter-Registry am Composition Root | Akzeptiert (2026-11-13) |
| [0057](0057-run-launcher-ports-adapters.md) | Run-Launcher — Port/Adapter für das Ausführungs-Substrat (`FABRIKANT_LAUNCHER`) | Akzeptiert (2026-11-18) |
| [0058](0058-webhook-replay-protection.md) | Webhook-Replay-Schutz (opt-in `replay_protection`) | Akzeptiert (2026-11-18) |
| [0059](0059-needs-human-escalation.md) | `needs human` — Escalation to a Human on Blockage, Stagnation, and Scope Drift | Akzeptiert (2026-11-19); Nachtrag 2026-11-16: Eskalationsart `access` (ADR 0060) |
| [0060](0060-research-fetcher-access-profiles.md) | Fetcher für offene Recherche (Herkunftsprüfung, Bereinigung/Injection-Scan), Zugriffsprofile pro Loop/Run, einmalige Freigaben | Akzeptiert (2026-11-19); Nachtrag 2026-11-22: In-Process-Detektor (Stufe C), Verdict gewährt nie Rechte, Redirect-Re-Freigabe |
| [0061](0061-replay-deterministic-reexecution.md) | Replay — deterministische Neu-Ausführung eines abgeschlossenen Runs (`fabrikant replay`, `replay_of`, Diff-Report) | Accepted (2026-11-21) |
| [0062](0062-hosting-provider.md) | Hosting Provider — Port/Adapter for Code Hosting (GitHub, GitLab) | Accepted (2026-11-21) |
| [0063](0063-hosting-provider-registry.md) | Hosting Provider Registry — Extensible Adapters | Accepted (2026-11-21) |
| [0064](0064-mcp-server-stdio.md) | MCP-Server — fabrikant als stdio-Endpoint für MCP-fähige Agenten (Runs, Specs, Approvals) | Accepted (2026-11-21); Amendment 2026-11-22: local profile, hardening, opt-in approvals; Amendment 2026-11-23: official MCP SDK, spec-write tools; re-landed 2026-11-25 |
| [0065](0065-golden-runs-eval-harness.md) | Golden Runs — gepinnte Baseline + Eval-Harness (`fabrikant eval`, opt-in CI-Gate) | Accepted (2026-11-21) |
| [0066](0066-spec-proposal-review-ui.md) | Spec-Proposal Review in the Web UI — server-side diff, apply/reject with roles + CSRF | Accepted (2026-11-24) |
| [0067](0067-template-library.md) | Template library — curated, documented loop templates + `fabrikant template` CLI | Accepted (2026-11-24) |
| [0068](0068-golden-run-store-mark.md) | Golden Runs — run-store mark + `fabrikant eval --golden <run-id>` | Accepted (2026-11-24) |
| [0069](0069-distribution-and-plugin-contract.md) | Distribution and Plugin Contract — `pip install fabrikant`, Docker, step plugins | Accepted (2026-11-24) |
| [0070](0070-rename-to-fabrikant.md) | Rename to fabrikant — package, CLI, env, entry points | Accepted (2026-11-24; amended: no legacy compat layer) |
