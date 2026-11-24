# ADR 0050: Agent-Env-Allowlist — Agent-Prozesse erben kein `os.environ`

**Status:** Vorgeschlagen
**Betrifft:** Security (Prompt-Injection-Exfiltration, OWASP ASI01/ASI03), One-Shot-CLI-Backends, `opencode serve`-Deployment.
**Issue:** #350.

## Kontext

Jeder Agent-Prozess (claude-code, codex, copilot) erbte bisher das komplette `os.environ`
des fabrik-Prozesses — darunter `GITHUB_TOKEN` (API-Bearer, 60-min-Installation-Token),
`GITHUB_REVIEWER_TOKEN` und LLM-Provider-Keys (`FABRIK_LLM_*_API_KEY`). Ein Agent ist per
Definition ein Prozess, der untrusted Input bearbeitet (Issues, PR-Kommentare, Web-Content):
Eine erfolgreiche Prompt-Injection kann ihn zu `printenv`/`env` auffordern und das Ergebnis
z. B. per HTTP-POST exfiltrieren. Die bestehende Secret-Redaction (ADR 0026) schützt nur
Logs, nicht die Prozess-Umgebung.

## Entscheidung

- **Allowlist-Basis:** Neues Modul `fabrik/agentenv.py`. `OneShotCliBackend.send_prompt`
  baut die Subprozess-Env jetzt über `build_agent_env()`: nur die Namen aus
  `AGENT_ENV_ALLOWLIST` (PATH, HOME, USER, TZ, XDG_*, … — 18 Namen, keine Secrets; bewusst ohne `PAGER`/`EDITOR`/`GIT_PAGER` als Befehls-Knöpfe und ohne `PWD`/`SHLVL`) werden
  aus `os.environ` übernommen.
- **Explizite Übergaben bleiben erlaubt:** `extra_env` (z. B. die `gh`-Identität eines
  `github_identity:`-Steps via `gitops.github_env`) und Adapter-Zusätze (`_env()`, z. B.
  `COPILOT_AUTO_UPDATE`) werden über die Allowlist gelegt und gewinnen. Das ist die
  beabsichtigte Grenze: was ein Step *bewusst* übergibt, darf der Agent sehen; was nur
  *geerbt* wird, nicht.
- **Adapter-Hook geändert:** `_env()` liefert jetzt Zusätze, nicht mehr einen
  `os.environ`-Klon. Jede CLI bekommt ihre **eigene Auth** explizit durchgereicht
  (`agentenv.passthrough`): claude-code `ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN`,
  codex `OPENAI_API_KEY`, copilot `COPILOT_GITHUB_TOKEN` (fine-grained PAT, „Copilot
  Requests“). Das ist die Arbeits-Identität der CLI, kein fabrik-Secret — ohne sie liefen
  die CLIs mit env-basierter Auth nicht mehr. **Nie** durchgereicht werden `GITHUB_TOKEN`/
  `GH_TOKEN` (fabriks API-Token); die bekommt ein Step nur per `github_identity:`.
- **Steps ohne `github_identity:`** (One-Shot-CLI-Backends) bekommen Allowlist, CLI-eigene
  Auth und die Projekt-Einstellungen (`subprocess_env()`: Token-Datei, `commit_via`). Ein
  GitHub-Token nur, wenn das Projekt eine Token-Datei (`github_env_file`) hat — dann das
  Builder-Token über `token_overrides()`; sonst keins (vorher geerbt). `opencode`-Steps
  betrifft das nicht: `opencode serve` ist ein eigener Prozess (siehe unten).
- **Operator-Ergänzung:** `FABRIK_AGENT_ENV_EXTRA` (kommagetrennte Namen) erweitert die
  Allowlist für Deployments, die dem Agent gezielt weitere (nicht sensible) Variablen
  geben wollen.
- **Geteilter `opencode serve`-Prozess:** Der wird nicht von fabrik gestartet, bekommt
  also die Allowlist nicht automatisch. Neu: `fabrik agent-env` druckt die gescrubte Env
  als `KEY=VALUE`-Zeilen — als systemd-`EnvironmentFile` oder Docker `--env-file` nutzbar.
  Deployment-Change-Liste steht im README (Agent-Backends).
- **`fabrik agent-env`** überspringt Variablen, deren Wert einen Zeilenumbruch enthält
  (würde im `EnvironmentFile`/`--env-file` Zeilen einschleusen).
- **Nicht betroffen:** Shell-Steps (`process_env`/`token_overrides` in `gitops.py`) —
  deren Env ist Operator-Konfiguration (Spec-`env:`), kein untrusted-Input-Expositionspfad;
  `gitops`-Prozesse brauchen ihre Token für die GitHub-API.

## Konsequenzen

- Eine Injection kann dem Agent-Prozess keine Credentials mehr über die eigene Umgebung ziehen
  (Umgehungspfade: siehe Restrisiko) — `env` zeigt nur ~23 harmlose Variablen. Pro-Step-GitHub-Identitäten bleiben vorhanden
  (der Agent braucht sie für `gh`). Ihr Umfang ist Sache der Token-Ausstellung: sie sollen
  least-privilege sein (Developer- bzw. Reviewer-Rolle, keine admin-PATs) — fabrik erzwingt
  das hier **nicht**.
- Backends, die unauffällig auf Env-Keys zugegriffen haben (z. B. Provider-Base-URLs aus
  der Umgebung), müssen diese jetzt über `FABRIK_AGENT_ENV_EXTRA` oder Step-Konfiguration
  beziehen — bewusste Konfiguration statt impliziter Erbschaft. Häufigster Fall:
  Corporate-Proxy — `FABRIK_AGENT_ENV_EXTRA=HTTPS_PROXY,HTTP_PROXY,NO_PROXY` (Proxy-URLs
  können `user:pass@` tragen, daher bewusst kein Default).
- Der geteilte `opencode serve`-Prozess bleibt ohne Deployment-Change anfällig;
  `fabrik agent-env` macht den sicheren Betrieb ein Befehl entfernt.

## Restrisiko

- **Dateien statt Env:** `HOME` bleibt in der Allowlist. Ein Agent als Host-Subprozess liest
  weiterhin Dateien des Betreibers (`~/.config/gh`, `~/.ssh`, `~/.config/fabrik/fabrik.env`,
  `FABRIK_ENV_FILE`, …) und kommt so an dieselben Tokens, die aus der Env entfernt wurden.
- **`/proc/<ppid>/environ`:** Ein Agent als Host-Subprozess kann die Env des fabrik-Parent-Prozesses
  (inkl. `GITHUB_TOKEN`) lesen; die Allowlist scrubbt nur die Env des Kindes, nicht die des Parents.
  Empirisch im Review (PR #521) reproduziert.
- Beide Pfade schließt dieses ADR **nicht**; die Grenze liefert erst die Per-Run-Sandbox
  (Issue #520, PR #523: kein Host-`/proc`-Mount, keine Host-`HOME`). Bis dahin ist die Allowlist
  Härtung gegen den naiven `printenv`-Pfad, keine Isolation.
- **Reichweite:** Gilt für die One-Shot-CLI-Backends. Das Standard-Backend `opencode`
  (geteilter Server) wird nur über `fabrik agent-env` + Deployment-Änderung erfasst.
