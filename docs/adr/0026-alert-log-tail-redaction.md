# ADR 0026: Redaction des Alert-`log_tail`

**Status:** Akzeptiert (2026-10-20, Merge #187; vorgeschlagen 2026-10-20). Folge-Issue
#132 zu ADR 0025
(dort bleibt der Ist-Stand „ohne Redaction“ als historischer Kontext stehen).

## Entscheidung

- `secrets.redact_text` maskiert pattern-basiert durch `[REDACTED]`:
  Bearer-Tokens, AWS-Keys (`AKIA…`/`ASIA…`), bekannte API-Key-Präfixe
  (`sk-`, `ghp_`, `github_pat_`, `xox*-`), Private-Key-Blöcke und generische
  `key=`/`token=`/`password=`/`secret=`-Assignments.
- Einziger Aufrufpunkt: `_notify` redigiert den Tail einmal; Slack, Webhooks und
  Alert-Issues/-Kommentare nutzen denselben Wert.
- Schalter `config.ALERT_LOG_REDACTION` (Default `True`).
- **Fail-safe:** wirft die Redaction, wird unredigiert gepostet + Warning
  geloggt — ein Alert geht nie verloren.

## Trade-offs

Heuristik: False Positives (zu viel maskiert) sind akzeptiert, False Negatives
(unbekannte Secret-Formate) bleiben möglich — die Ops-only-Sichtbarkeit aus
ADR 0025 gilt weiterhin.
