# ADR 0032: ADR-Nummern — Vergabe, Kollisionsprüfung, Umnummerierung

**Status:** Akzeptiert (2026-10-20, Merge #252; vorgeschlagen 2026-10-20)
**Herkunft:** Issue #134, PR #204 (dort als „ADR 0019“, selbst eine Kollision mit
0019-pause-resume-disable). #204 wurde fälschlich als Duplikat von #201 geschlossen;
#201 enthielt nur eine README-Änderung, Script, ADR und Merge-Check gingen verloren.

## Kontext

Agents wählen die ADR-Nummer als „nächste freie in `docs/adr/`“, und zwar im Stand des
eigenen Branches. Parallele Branches kollidieren dadurch zwangsläufig. Das gilt für
develop und für gestapelte ADR-PRs aus pr-reply (ADR 0011). Verweise wie „ADR 0013“
werden dann mehrdeutig. Feldbefund 2026-10-20:

- Auf `main` waren 0013, 0014 und 0018 je doppelt, 0023 dreifach belegt.
- Drei offene PRs beanspruchten 0024/0025.
- Code verwies noch auf Branch-Nummern: „ADR 0006“ für Dual-Identity, gemergt als 0010.

## Entscheidung

1. **Script `scripts/check-adr-numbers.py`** (Muster: `scripts/pr-status.py`), Base
   `origin/main`:
   - `check`: im Branch **neue** ADRs mit bereits auf der Base vergebener Nummer
     → Exit 1. Nur gegen die Base, damit deterministisch.
   - `next`: höchste Nummer auf der Base **und in allen offenen PRs** + 1. Der PR des
     eigenen Branches zählt nicht. Ohne `gh` gilt mit Warnung nur die Base.
   - `fix`: benennt nur kollidierende **neue** ADRs um, dabei auch an Nummern offener
     PRs vorbei. Umbenannt werden Dateiname, Titelzeile und Dateipfad-Verweise;
     `ADR NNNN` nur in vom Branch hinzugefügten Zeilen. ADRs der Base und
     Laufzeitdaten (`runs/`, `state/`) werden nie angefasst.
2. **Merge-Gate:** `pr-status.py merge-check` liefert `wait`, solange der PR eine ADR mit
   einer auf der Base vergebenen Nummer anlegt. Das fängt auch Kollisionen zwischen zwei
   PRs, denn nach dem Merge des ersten liegt dessen Nummer auf der Base. So landet nie
   eine Doppel-Nummer auf `main`.
3. **develop-Loop:** Shell-Step `check-adr` (`fix` → `check`) nach dem Agent, vor
   lint/test/ship. Deterministisch, nicht vom Prompt abhängig; die Reparatur wird
   mitgetestet und committet.
4. **Prompts:** develop und pr-reply holen die Nummer per `next`. pr-reply hat keine
   Shell-Steps und führt nach dem Anlegen bzw. nach einem Rebase selbst `fix` und dann
   `check` aus.
5. **CI-Invarianten** (`tests/test_adr_index.py`):
   - Nummern sind eindeutig.
   - Die Titelzeile passt zum Dateinamen.
   - Der Index ist vollständig und sortiert.
   - Jeder `ADR NNNN`-Verweis zeigt auf eine vorhandene ADR.
6. **Umnummerierung bei Altlasten:** Die zuerst gemergte ADR behält ihre Nummer; die
   übrigen rücken in Merge-Reihenfolge ans Ende. Verweise werden nach Kontext
   umgestellt, nicht per Suchen/Ersetzen.
7. **Lücken bleiben Lücken:** Eine einmal auf `main` vergebene Nummer wird nie neu
   vergeben (0007: approval-request-protocol, 2026-10-18 → 0011). Sonst würden alte
   Verweise in Commits, Issues und Run-Logs mehrdeutig.

## Konsequenzen

- Von zwei parallelen PRs wird der zweite nach dem Merge des ersten umnummeriert. Den
  Anstoß gibt `merge-check`, die Reparatur macht `fix`.
- `ADR NNNN`-Verweise in vom Branch hinzugefügten Zeilen gelten als Verweis auf die
  umbenannte ADR. Im Kollisionsfall sind Verweise auf die gleichnamige Base-ADR nicht
  unterscheidbar; das bleibt selten und muss im Review geprüft werden.
- Der Verweis-Check findet nur nicht existierende Nummern. Ein Verweis auf die
  *falsche*, aber existierende ADR (der 0006/0010-Fall) bleibt Review-Sache.
- Haben zwei *neue* ADRs dieselbe Nummer, verschiebt `fix` die alphabetisch spätere.
  Auf einem Umnummerierungs-Branch gelten auch die verschobenen ADRs als neu; dort
  `fix` nicht blind laufen lassen.
- Nummern spiegeln die Merge-Reihenfolge nur ungefähr wider; die ADR-Daten im Header
  sind maßgeblich.
