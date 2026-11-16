# ADR 0018: Nativer `issue`-Step

**Status:** Akzeptiert (2026-10-20, Merge #184; vorgeschlagen 2026-10-20)
**Ergänzt:** ADR 0001 §5 (`gh`-CLI als Übergang bis der `issue`-Step existiert).

## Kontext

Triage-/Refine-/Sweep-Loops machten Issue-Operationen per `shell` + `gh`. Das ist
fragil (Substitution im Shell-String, ADR 0008 braucht `env:`-Workarounds,
mktemp/trap-Boilerplate) und die API-Aufrufe sind nicht isoliert testbar.

## Entscheidung

- Neuer deterministischer Step-Typ `issue` (`create|edit|label|close`) auf Basis der
  vorhandenen GitHub-REST-Helfer (`_gh_api`, `GITHUB_TOKEN`, wie `pr`/`pr_merge`).
  Werte gehen als JSON-Payload an die API, nie durch eine Shell.
- `create` setzt `issue_number`/`issue_url` im Variable-Store.
- Bedingte Ausführung step-lokal über `only_if` (gleiche Semantik wie `approval_when`,
  geteilter Helfer `_cond_matches`) und `skip_if_empty`; `edit` unterstützt
  `append` + `unless_contains` (Idempotenz-Marker statt Shell-`case`).
- `label` legt fehlende Labels implizit an (GitHub-Verhalten). Die bisherigen
  `ensure-labels`-Steps (`gh label create --color`) entfallen; Label-Farben müssen
  einmalig manuell gesetzt werden. Bewusst kein fünfter Action-Typ.
- Kyudo-Triage, fabrik-Refine und refactor-sweep nutzen `issue`-Steps. Der Sweep-Agent
  liefert nur noch JSON-Kandidaten; ein `llm`-Step extrahiert sie, max. 3
  `create`-Steps legen die Issues an. Der ai-lab-Spec bleibt auf `shell` + `gh`
  (außerhalb des Scopes).
- Token-Schutz: `GITHUB_TOKEN` erscheint nie in Step-Ergebnissen; zusätzlich greift das
  bestehende Secret-Masking in Logs und Run-Records.

## Folgen

- Weniger Shell-Injection-Fläche, testbare Issue-Logik ohne `gh`.
- Der ai-lab-Spec kann später analog migriert werden.
