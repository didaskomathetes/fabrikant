# ADR 0022: Modellwahl ist Konfiguration, keine Architektur

**Status:** Akzeptiert (2026-10-19)
**Entscheider:** Dominic
**Ersetzt:** ADR 0001 §3, Absatz 2 (Bindung von `pr-reply`/`review-pr` an
konkrete Modelle bzw. an `FABRIK_AGENT_MODEL`) und die Spalte „Modell“ der
Loop-Tabelle in ADR 0001. Die Invariante Builder ≠ Reviewer (ADR 0001 §3,
ADR 0003) gilt unverändert.

## Kontext

ADR 0001 legte fest, wie die Loops an Modelle gebunden sind: `pr-reply` über
das Env `FABRIK_AGENT_MODEL`, `review-pr` über die Copilot CLI. Modelle und
Backends ändern sich schnell. Copilot zieht Modelle zurück, neue Generationen
kommen, Preise ändern sich. Jeder Wechsel lief damit gegen den Text einer ADR,
obwohl er keine Architektur ändert. Beispiel: `develop`/`pr-reply` wechseln
von opencode (Env-Default) zu `claude_code` mit explizitem Step-Modell.

## Entscheidung

- **Builder ≠ Reviewer bleibt Architekturentscheidung** (Leitprinzip 4,
  ADR 0001 §3, ADR 0003): Builder und Reviewer nutzen immer verschiedene
  Modelle. Welche Modelle das sind, ist Konfiguration — dass sie
  verschieden sind, nicht. `review`/`pr_review` prüfen das zur Laufzeit
  (Cross-Model-Check, failt bei gleichem Modell); der Loop `review-pr`
  (agent-Step) hält die Invariante über den Anker `reviewer_model`. Eine
  Aufhebung der Invariante braucht eine neue ADR.
- **Modell und Backend pro Step sind Konfiguration.** Sie stehen in der Spec
  (`projects/*.fabrik.yaml`) oder in `fabrik.env`, nicht in ADRs. Ein Wechsel
  braucht keine ADR und keine ADR-Freigabe.
- **ADRs benennen Rollen, keine Modelle:** Builder, Reviewer, Klassifikator.
  Welche Rolle welches Modell nutzt, bestimmt die Spec.
- **Rollen teilen ein Modell über YAML-Anker**, wie eine lokale Variable:
  in `projects/fabrik.fabrik.yaml` nutzen `develop` und `pr-reply`
  `*builder_backend`/`*builder_model`, `develop.review` und `review-pr`
  `*reviewer_model`.

## Konsequenzen

- Modellwechsel sind normale Spec-Änderungen. Sie laufen durch die üblichen
  PR-Gates, aber ohne ADR-Freigabe.
- ADR 0001 §3 bleibt als Begründung der Rollen erhalten. Konkrete Bindungen
  dort sind historisch.
- Deployment-Voraussetzungen (CLI im PATH, Tokens) dokumentiert
  `docs/backends.md`, nicht die ADR.
