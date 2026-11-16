# SonarQube (lokal)

`compose.yaml` startet SonarQube Community **mit dem
[Community-Branch-Plugin](https://github.com/mc1arke/sonarqube-community-branch-plugin)**
(Image `mc1arke/sonarqube-with-community-branch-plugin`): Branch- und PR-Analysen sowie
PR-Dekoration auf GitHub, die in der Community Edition sonst fehlen.

```bash
docker compose up -d        # http://localhost:9000, Erst-Login admin/admin
```

**Version pinnen:** Plugin-Version = SonarQube-Minor (z. B. Plugin 26.5 ↔ SonarQube 26.5.x).
SonarQube kann seine DB nicht downgraden. Beim Upgrade (`SONARQUBE_TAG`) auch das Volume
`fabrik_sonarqube_extensions` löschen, da es eine Kopie der Plugin-JAR enthält.

## Scannen

`scripts/sonar-scan.sh` (Tests + Coverage → Analyzer-Reports → pysonar → Quality Gate):

| Aufruf | Analyse |
|---|---|
| `bash scripts/sonar-scan.sh` | Main-Branch (Loop `sonar-scan`, täglich) |
| `SONAR_BRANCH=auto bash scripts/sonar-scan.sh` | aktueller Git-Branch, lokal vor dem Push |
| `SONAR_PR=42 bash scripts/sonar-scan.sh` | PR #42 (vorher `gh pr checkout 42`) |
| `SONAR_PR=auto bash scripts/sonar-scan.sh` | PR des aktuellen Branches |

`SONAR_REPORT_FILE=<pfad>` schreibt das Ergebnis (Gate, Kennzahlen, offene Issues) zusätzlich als
JSON, auch bei rotem Gate.

**Analyzer-Reports:** Vor dem Scan schreibt `scripts/lint.sh --reports .fabrik/sonar/lint` die Funde von
Ruff, Bandit (SAST) und Pyright; Sonar zeigt sie als externe Issues (Quality Gate: zählen als New Issues).
Ruff und Bandit importiert Sonar-Python nativ (`sonar.python.{ruff,bandit}.reportPaths`); Pyright kennt
es nicht, daher konvertiert `scripts/pyright_sonar.py` ins Generic Issue Format
(`sonar.externalIssuesReportPaths`). Report-Funde lassen den Scan nicht scheitern — das Gate dafür ist
`scripts/lint.sh` ohne Argumente (CI, `lint`-Step von develop/refactor). „New Code“ eines Branches/PRs wird gegen die Analyse der Ziel-Branch
gerechnet. Deshalb läuft `sonar-scan` täglich auf `main`.

## PR-Kommentare (Loops)

Ohne GitHub-Checks-Permission, über die vorhandenen GitHub-Apps (ADR 0010):

- **`sonar-pr`** (`on_pr`, Poll 30 min, pro Head-SHA = entprellt: Pushes eines Intervalls → ein Scan des
  letzten Stands): PR-Analyse (`SONAR_PR`, Head-Checkout im Skript) →
  `scripts/sonar_report.py` rendert Report und Kommentar aus den API-Antworten (Template, kein
  LLM) → `scripts/gh-upsert-comment.sh` postet ihn als **Reviewer-App** (`pull_requests: write`
  reicht). Pro PR genau ein Kommentar
  (Marker `<!-- fabrik-sonar -->`), jeder Push aktualisiert ihn. Ein rotes Gate wird erst
  kommentiert, dann schlägt der Run fehl (`outcome`).
- **`token-refresh`** (alle 30 min + `on_start`): erneuert die 1h-Tokens beider Apps, solange der
  fabrik-Server läuft. Ersetzt die Cron-Zeilen aus dem README.

Der Kommentar ist ein normaler PR-Kommentar, kein Review: Er setzt kein Verdikt und blockiert
`review-pr` nicht. `pr-reply` sieht ihn einmal pro PR als neuen Kommentar.

### Alternative: Plugin-Dekoration

Das Branch-Plugin kann PRs selbst dekorieren (Check Run mit Annotationen + Summary-Kommentar).
Das braucht eine GitHub-App mit **Checks: Read & write**. Ohne diese Permission scheitert schon
der Check Run, und der Kommentar fehlt ganz. Einrichtung: `scripts/sonar-github-setup.sh`
(Doku im Skript-Kopf). Wer das Projekt so bindet, sollte `sonar-pr` abschalten, sonst kommentieren
beide.

## Andere Projekte

`sonar-scan.sh` scannt per Env jedes Repo: `SONAR_PROJECT_DIR` (Worktree, z. B. `.`),
`SONAR_PROJECT_KEY`, `SONAR_SCANNER` (`{fabrik_home}/.venv/bin/pysonar`, wenn das Projekt pysonar
nicht als Dependency hat), `SONAR_SCANNER_ARGS` (`-Dsonar.…`, ersetzt `[tool.sonar]`),
`SONAR_TEST_CMD` + `SONAR_COVERAGE_PATHS`, `SONAR_LINT_CMD` (schreibt `ruff.json`, `bandit.json`,
`pyright-sonar.json` nach `$SONAR_LINT_DIR`; ohne Angabe für fremde Projekte kein Import). uv-Monorepos mit CI-Testmatrix: `scripts/ci-matrix-pytest.py
--cov-dir D` (`--all` = ganze Matrix, sonst nur vom Diff betroffene Pakete) schreibt `D/coverage-<n>.xml`.

Beispiel: die Loops `sonar-main` (täglich) und `sonar-pr` (Opt-in per PR-Label `sonar`, entprellt,
Kommentar erst nach Freigabe)
in der ai-lab-merchant-assistant-Spec (`FABRIK_PROJECTS_PATH`). `gh-upsert-comment.sh` erkennt den eigenen
Kommentar am Login des Tokens (PAT) bzw. am Bot-Typ (App-Token).

Bringt das Projekt ein eigenes Scan-Skript mit (eigene `sonar-project.properties`, Linter-Importe), ruft
der Loop dieses als `SONAR_SCANNER` auf, mit `SONAR_TEST_CMD=true` und leerem `SONAR_COVERAGE_PATHS`.
Das Skript muss seine Argumente (`-Dsonar.branch.name` / `-Dsonar.pullrequest.*`) an den Scanner
weiterreichen.
