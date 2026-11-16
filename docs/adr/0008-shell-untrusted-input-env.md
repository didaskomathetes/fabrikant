# ADR 0008: Shell-Steps mit untrusted Input — `env:` statt Inline-Substitution

**Status:** Accepted (2026-10-18) — Amendment (Guard-Semantik + Kompatibilität) in Draft, PR #72; geändert 2026-11-03 (PR #347): Trust-Default für `extra_context`-Quellen

## Kontext

Shell-Steps (`type: shell`) ersetzen `{Platzhalter}` im `cmd` per
`_substitute()` (Variable-Substitution aus dem Run-Kontext). Einige dieser
Variablen sind **untrusted**: LLM-Output (z. B. Triage-`acceptance_criteria`),
Issue-Titel/-Body.

Wenn untrusted Text **inline** in den Befehl substituiert wird, brechen
Quotes/Backticks/Newlines/`$`-Sequenzen die Shell-Semantik:

- **Stilles No-op:** Ein Newline/Quote im Wert zerhackt eine `if`-Bedingung →
  der Guard wird `false` → `exit 0` → der Step meldet "pass", obwohl nichts
  passiert ist.
- **Command-Substitution:** Backticks (`` `…` ``) oder `$(…)` im Wert werden
  von der Shell **ausgeführt** → Shell-Injection.

Regression: Issue #62 (Triage-Loop `add-acceptance` substituierte
LLM-`acceptance_criteria` inline).

## Entscheidung

**Untrusted Werte gehören ausschließlich in `env:`, nicht in `cmd`.**

1. **`env:`-Mechanismus:** Untrusted Werte werden als Umgebungsvariable
   übergeben und im Befehl als `"$VAR"` referenziert. Die Shell behandelt den
   Wert als ein einzelnes, inertes Argument — keine Interpretation von
   Quotes/Backticks/`$`.

   ```yaml
   env:
     criteria: "{acceptance_criteria}"
   cmd: 'if [ -n "$criteria" ]; then …; fi'
   ```

2. **Fail-fast-Guard gegen das Mischmuster:** Ein Step, in dem ein
   Inline-Placehalter in `cmd` **mit einem `env:`-Key desselben Steps
   kollidiert**, wird **abgelehnt** (`ok: False`, `exit_code: 1`) — nicht nur
   gewarnt. Eine Warnung ist ignorierbar und die Inline-Substitution würde
   trotzdem laufen; ein fehlgeschlagener Step ist nicht. Die Kollision ist die
   eigentliche Gefährdung: der Platzhalter wird aus dem (potenziell untrusted)
   Kontext substituiert, während der `env:`-Wert ignoriert wird.
   Vertrauenswürdige Placehalter (`{issue_number}`, `{run_id}`, …) ohne
   Kollision mit `env:`-Keys bleiben erlaubt — auch in Steps, die `env:` für
   untrusted Werte nutzen. Die Platzhalter-Erkennung nutzt eine
   Regex (`\{[A-Za-z_][A-Za-z0-9_]*\}`, gleiche Syntax wie `_substitute`),
   damit Literal-Braces (z. B. JSON `{"a": 1}`) **kein** False-Positive
   triggern.

3. **Log-Masking:** Der Step loggt das **Command-Template** (mit
   `{placeholders}`), nicht den substituierten Befehl — substituierte Werte
   können Secrets/untrusted Text enthalten und dürften nicht im Run-Log landen.

4. **Trust-Default für `extra_context`-Quellen** (PR #347): Jede Quelle gilt als
   untrusted, solange sie nicht `trusted: true` setzt. Das ist nur für repo-eigene
   Dateien und Tool-Ausgaben vorgesehen; Issue-Text und PR-Kommentare bleiben untrusted.
   Untrusted Inhalt geht durch `untrusted_blocks`. Darin werden `<untrusted`/`<context`-Tags
   neutralisiert, damit Text keinen Block schließen oder einen trusted Block vortäuschen kann.

## Konsequenzen

- Deterministisches Sicherheitsverhalten: das unsichere Mischmuster schlägt
  fail-fast fehl, statt still ein No-op zu sein oder zu injizieren
- Kein Secret-Leak im Run-Log (Template statt substituiertem Befehl)
- Untrusted Text bleibt byte-gleich erhalten (kein stilles No-op)
- Regex-Erkennung = gleiche Platzhalter-Syntax wie `_substitute` (konsistent,
  keine False-Positives bei Literal-Braces)
- Bestehende Shell-Steps mit inline untrusted Werten müssen auf `env:`
  migriert werden (sonst fail-fast)
- `env:`-Werte werden per `_substitute` aufgelöst — vertrauenswürdige
  Config-Werte (z. B. `{issue_number}`) dürfen weiterhin inline in `cmd`
  stehen, solange sie nicht mit `env:`-Keys desselben Steps kollidieren

## Kompatibilität & Migration

Die Platzhalter-Regex ist bewusst **strikt**: `\{[A-Za-z_][A-Za-z0-9_]*\}`
verlangt einen echten Identifikator (erster Buchstabe/`_`). Das ist ein
**Breaking Change** gegenüber der früheren, lockeren Syntax:

- **Numerische/sonstige Platzhalter** wie `{123}` werden **nicht mehr
  substituiert** — sie bleiben literal im Befehl stehen.
- **Migration:** Betroffene Steps (falls vorhanden) müssen die Platzhalter auf
  Identifikator-Form umstellen (z. B. `{123}` → `{num123}` bzw. den
  Kontext-Key entsprechend benennen). Im aktuellen Projekt (`projects/*.yaml`)
  existieren keine numerischen Platzhalter — die Migration ist dort ein No-op.

Der Guard selbst ist **relaxiert** (keine Breaking-Change): Steps mit `env:`
und Inline-Placeholdern, die **nicht** mit `env:`-Keys kollidieren, laufen
weiterhin — zuvor wurden sie pauschal abgelehnt.

## Alternativen (abgelehnt)

- **Warn-only:** ignorierbar; unsichere Fälle laufen trotzdem → inkonsistentes
  Sicherheitslevel.
- **Nur CI-Lint-Regel:** fängt nur statisch erkannte Fälle, kein
  Laufzeit-Schutz; weniger direkt als ein fail-fast-Step.
- **Shell-Quoting der substituierten Werte:** verhindert nicht das Muster,
  ist fehleranfällig (Quoting korrekt zu machen ist genau das Problem) und
  ändert die Semantik legitimer Inline-Nutzung.
