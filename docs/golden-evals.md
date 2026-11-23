# Golden Runs & Eval

Golden Runs pinnen das **stabile Ergebnis** eines Loops als reviewbare,
committed Baseline und vergleichen nach einer Pipeline-/Spec-Änderung, ob
der Loop regressiert. ADR 0065 (Harness) + ADR 0068 (Run-Store-Markierung
+ `eval --golden`).

## Konzept

- **Golden** = `projects/<projekt>.golden.json`, liegt *neben der Spec* und
  ist ein Source-Control-Artefakt: reviewbar, diffbar, versioniert wie Code.
  Eine alte Baseline ist eine sichtbare Änderung, kein stiller Drift.
- Erfasst wird nur die **stabile, entscheidungsrelevante Oberfläche**:
  Run-`status` + pro Step `status`/`verdict`. Niemals volatile LLM-
  Freitexte (`summary`), Laufzeiten oder Token-Zahlen — die würden das
  Gate flaky machen.
- **Eval** = echte Re-Execution des Loops mit den gepinnten Eingaben des
  Baseline-Runs (via Replay, ADR 0061). `diffs == []` → keine Regression.
  Geänderter `spec_hash` ist *erwartet* (genau dann evalt man) und kein
  Fehler — nur der Ergebnis-Vergleich entscheidet.

## Workflow

### 1. Baseline fangen (capture)

Nach einem bekannten, abgenickten Run:

```sh
fabrikant golden capture <project> <loop> <run-id> [--name N]
```

Legt `projects/<projekt>.golden.json` an (Default-Name = Loop-Name).
**Best-effort-Markierung:** der Run-Record im Store bekommt `golden = <name>`
(ADR 0068). Ohne Run-Store (z. B. CI-Runner ohne `runs/`) wird die
Markierung übersprungen — die Sidecar-Datei bleibt die Review-Quelle.

### 2. Baseline ansehen (show)

```sh
fabrikant golden show <project> [--name N]
```

Listet die Goldens eines Projekts (Name, Loop, Baseline-Run, `spec_hash`,
Erwartung).

### 3. Eval laufen

Zwei äquivalente Formen:

```sh
# explizit
fabrikant eval <project> <loop> [--name N] [--yes]

# über den Baseline-Run (ADR 0068) — Project/Loop/Name aus dem Store
fabrikant eval --golden <run-id> [--yes]
```

Die Eval re-exekutiert den Loop (alle Steps, Side Effects inklusive —
wie Replay) und druckt einen Report: geänderter `spec_hash`, Status-/
Verdict-Abweichungen pro Step, `diffs`.

**Exit-Codes (CI-Vertrag):**

| Code | Bedeutung |
| ---- | --------- |
| `0`  | keine Regression (Gate grün) |
| `1`  | Regression — stabile Oberfläche driftet zur Baseline |
| `2`  | Infrastruktur-/Usage-Fehler oder inkonklusiv (z. B. wartet auf Freigabe) |

Ohne `--yes` fragt die Eval vor der Re-Execution zurück (Pflicht ohne TTY,
da es eine echte Re-Execution mit Side Effects ist).

## CI-Gate (opt-in)

Das Eval läuft **nicht** bei jedem Push/PR. Es ist ein auf-demand,
blockierendes Statement: „diese Pipeline-Änderung hat meine Loops nicht
regressiert". Das Workflow-File liegt staged unter
[`ci/golden-eval.yml`](ci/golden-eval.yml) und wird erst nach Freigabe der
`workflows`-Write-Permission der contributing App nach
`.github/workflows/golden-eval.yml` gezogen (ADR 0065, Gap 3).

## Re-Baseline

Eine Regression (Exit `1`) erzwingt eine bewusste Entscheidung: Entweder
die Änderung ist korrekt → neu fangen
(`fabrikant golden capture …`), oder die Änderung ist ein Bug → rückgängig
machen. Ein Golden ist nie „still veraltet" — der Drift ist ein sichtbarer,
blockierender Report.
