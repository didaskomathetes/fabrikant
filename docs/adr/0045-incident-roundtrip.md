# ADR 0045: Incident-Roundtrip (Sentry/CloudWatch → Triage → Hotfix-PR)

**Status:** Akzeptiert (2026-11-13)
**Bezug:** `docs/framework-v1-draft.md` Phase 3 + §4, ADR 0027 (`on_webhook`), ADR 0011 (Approval),
Issue #33 (`on_slack`), #34 (`on_datadog`).

## Kontext

Phase 3 verlangt den Zyklus Alert → Triage → Workflow → Hotfix-PR → Verify → Merge.
Alert-Quellen sind Sentry und CloudWatch. `on_webhook` (ADR 0027) liefert Auth und
Payload→Variablen, kennt aber keine Alert-Formate und keine Triage.

## Entscheidung

- **Intake:** neuer Trigger `on_alert` (`source: sentry|cloudwatch`, `secret_env`) und Route
  `POST /api/alert/<projekt>/<loop>`. Auth wie ADR 0027 (HMAC-SHA256 über den Raw-Body,
  `X-Fabrikant-Signature`; für Sentry zusätzlich `Sentry-Hook-Signature`); alles andere → einheitlich
  401. Die Route ist wie der Webhook von Origin/Token ausgenommen und über den Tunnel-Host erreichbar.
  Payloads werden in `alerts.Alert` normalisiert und als `alert_*`-Variablen über die gemeinsame
  Run-Start-Funktion (`_start_run`) an den Triage-Loop gegeben. CloudWatch-Zustand ≠ `ALARM` →
  200 `ignored`, kein Run. Ungültiges JSON 400, fehlende Pflichtfelder 422.
- **Triage:** ein normaler Loop mit `llm`-Step (read-only: kein Tool-/Dateizugriff, Alert-Felder
  als `untrusted`). Das Schema erzwingt `category ∈ {bug, infra, noise, unknown}` (enum, Default
  `unknown`) — das LLM klassifiziert nur.
- **Deterministisches Mapping:** neuer Step `alert_route` bildet `category` per Spec-Tabelle
  (`routes: {bug: hotfix}`) auf einen Loop desselben Projekts ab. Gleiche Kategorie → gleicher Loop;
  keine LLM-Entscheidung über den Zielworkflow. Ungemappte/`unknown`-Kategorien → `fallback`
   (Default `none`: kein Hotfix, Run endet `pass` mit Vermerk). Selbst-Routing und
   Route-Zyklen über mehrere Loops (z. B. `a → b → a`) werden beim Spec-Load abgelehnt —
   sie würden sonst unendlich verschachtelte Runs starten.
- **Hotfix:** Der Ziel-Loop läuft synchron als eigener Run (`trigger: alert:<triage-run>`, eigener
  Record, eigene Tier-/Approval-Gates) mit `alert_*` + `category`. Typisch: Fix (agent) → `verify` →
  `pr`. Merge bleibt hinter den bestehenden Gates (`pr_review`, `human_gate`, `pr_merge`).
  Wartet der Hotfix auf Freigabe (`needs_approval`), gilt der Triage-Run als erfolgreich.
- **Abgrenzung:** #33 (`on_slack`) und #34 (`on_datadog`) sind weitere Intake-Quellen; sie können
  `alerts.Alert` / `alert_route` wiederverwenden, sind aber nicht Teil dieser Entscheidung.
  Kein Replay-Schutz (wie ADR 0027).

## Konsequenzen

Triage-Runs belegen einen Worker, bis der Hotfix-Run endet. Dedup wiederkehrender Alerts
(`alert_fingerprint` steht bereit) ist nicht Teil dieses Schritts.

Der Hotfix-Run wird direkt per `run_loop` gestartet (nicht über den Worker-Pool/`claim_run`)
und zählt daher **nicht** gegen `max_parallel` — bewusste Entscheidung: Der Triage-Run
blockiert seinen Worker ohnehin bis zum Hotfix-Ende, eine zusätzliche Slot-Buchung würde
nur die `max_parallel`-Semantik verkomplizieren, ohne zusätzliche Kapazität zu schützen.
