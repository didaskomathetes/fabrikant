# ADR 0054: Per-Run-Agent-Sandbox — One-Shot-CLI-Agenten in einem Docker-Container

**Status:** In Überarbeitung (2026-11-06) — Design der Mechanik steht und ist umgesetzt (opt-in);
offen sind das Sandbox-Image und die Host-Ausführung von Repo-Code (s. „Offene Punkte“).
Ursprünglich unter der Nummer 0052 vorgeschlagen; umnummeriert, weil die Nummer an „Benannte Verbindungen“ (PR #508) ging.
Am 2026-11-06 überarbeitet: Austausch über git-Bundles statt Worktree-Mount.
**Betrifft:** Security (Code-Ausführung, OWASP ASI05), One-Shot-CLI-Backends, Deployment.
**Issue:** #520 (ersetzt/verfeinert #20 „Worker Service + Per-Run Sandbox").

## Kontext

Eine injizierte Anweisung lässt den Agent-Befehl **executieren** — auf dem Host.
Der One-Shot-Subprozess (claude-code/codex/copilot/opencode) läuft mit den Rechten des
fabrikant-Betreibers, sieht das gesamte Dateisystem und (ohne ADR 0051) das Internet.
Prompt-Defense begrenzt die Wahrscheinlichkeit, nicht den Schaden. Die Kontrolle
gehört in die Ausführungsebene: jeder Agent-Run ist ein isolierter Container.

Der erste Entwurf mountete den Per-Run-Worktree schreibbar in den Container. Das hat zwei
Fehler: Die `.git`-Datei eines Worktrees zeigt auf `<repo>/.git/worktrees/<id>` auf dem
Host — im Container gibt es den Pfad nicht, git funktioniert dort nicht. Und der Agent kann
`/work/.git` durch ein eigenes `.git`-Verzeichnis ersetzen (z. B. `core.fsmonitor=<Befehl>`);
das nächste `git status` von fabrikant auf dem Host führt den Befehl dann mit fabrikants Rechten
aus. Das Haupt-`.git` mit zu mounten, gäbe dem Agenten Hooks und Config des echten Repos.

## Entscheidung

Mit `FABRIKANT_SANDBOX=1` hüllt `OneShotCliBackend.send_prompt` den Agent-Befehl in
`docker run` ein (`sandbox.py`):

- **Isolation:** eigener Container pro Run (`fabrikant-agent-<sid>`, `--rm`, `--init`),
  `--network fabrikant-sandbox` (ADR 0051: `internal` — kein direkter Internetzugang).
- **Hardening:** `--cap-drop ALL`, `--security-opt no-new-privileges`, `--pull never`.
  Ausführungs-User = host uid:gid; **läuft fabrikant als root, ein unprivilegierter User
  (10001:10001)** — root im Container ist ohne userns-remap root-uid auf dem Host. Anderer User
  über `FABRIKANT_SANDBOX_USER`.
- **Resource-Caps:** `--memory 4g` (= `--memory-swap`, kein Swap), `--cpus 2`, `--pids-limit 512`, tmpfs `/tmp` und `/home/agent` je 256m
  (Fork-Bombs / DoS gegen den Host sind damit begrenzt).
- **Dateisystem:** read-only root fs; tmpfs für `/tmp`, `/home/agent` (HOME) und den
  Arbeitsklon `/work` (`exec`, `FABRIKANT_SANDBOX_WORK_SIZE`, Default 2g — zählt gegen `--memory`).
  **Kein Mount des Worktrees.**
- **Austausch über git:**
  0. *Voraussetzung:* `directory` muss die Wurzel eines Git-Worktrees sein (sonst enthielte das
     Bundle ein übergeordnetes Repo und der Patch träfe falsche Pfade) — sonst scheitert der Step.
  1. *Hinein:* fabrikant schreibt den Stand des Worktrees (HEAD + uncommittete + untracked
     Änderungen, ohne ignorierte Dateien) als Commit über einen temporären Index — Index,
     HEAD und Dateien des Worktrees bleiben unverändert — und als `git bundle` in ein
     privates Temp-Verzeichnis. Das Bundle wird read-only als `/in/base.bundle` gemountet.
  2. *Im Container:* Der Entrypoint (POSIX sh, braucht git im Image) klont das Bundle nach
     `/work/repo`; der Agent arbeitet dort mit vollem eigenem git. Danach `git add -A`,
     Commit, Bundle nach `/out/result.bundle` — `/out` ist ein leeres Austauschverzeichnis
     und das einzige schreibbare Mount. git-Ausgaben gehen nach stderr, stdout gehört dem
     Agenten (Prompt via stdin, Antwort via stdout wie ohne Sandbox).
  3. *Zurück:* fabrikant übernimmt das Ergebnis nur nach Prüfung: `result.bundle` muss eine
     reguläre Datei sein (kein Symlink), `git fetch` mit `fsckObjects`, das Ergebnis muss
     vom Basis-Commit abstammen, kein Pfad darf eine `.git`-Komponente haben, keine Symlinks
     (Modus 120000) und keine Gitlinks/Submodule, Bundle ≤ 512 MiB, Patch ≤ 128 MiB. Vor dem
     Einlesen wird der Container beendet (`docker rm -f`). Dann wendet
     fabrikant den **auf dem Host berechneten** Diff per `git apply` an (ohne externe
     Diff-Treiber/textconv). Der Worktree sieht danach aus wie nach einem Agenten auf dem
     Host, der nicht committet; die Pipeline danach (Scope-Guard, Lint, Tests, Commit/Push
     durch fabrikant) bleibt unverändert. Auch bei Exit ≠ 0 wird übernommen (wie bisher bleiben
     Änderungen eines gescheiterten Agenten liegen); ein abgewiesenes Ergebnis lässt den
     Step scheitern.
- **Push aus dem Container ist nicht vorgesehen, aber nicht verhindert:** Der Austausch braucht
  weder Netz noch Token, und fabrikant pusht nach den Prüf-Steps. Hat der Step jedoch ein
  `github_identity:`-Token und ist `github.com` erlaubt (ADR 0051), kann der Agent im Umfang
  dieses Tokens selbst pushen. Die Grenze ist der Token-Umfang, nicht die Sandbox.
- **Env:** nur die bereits gescrubte Allowlist-Env (ADR 0050) als explizite
  `-e KEY`-Flags — ohne Wert im argv (sonst sichtbar in `ps`/`docker inspect`); der Wert kommt
  aus der Env des docker-CLI-Prozesses, der zusätzlich `DOCKER_*`/`XDG_RUNTIME_DIR` zum Finden
  des Daemons erhält (gelangen nicht in den Container) — Host-Pfad-Variablen (HOME, PATH, XDG_*, TMPDIR, …) werden
  gestrippt, Container-Defaults (`HOME=/home/agent`, XDG_* darunter) gewinnen
  (Docker: letztes `-e` gilt). Optional `HTTP(S)_PROXY` (ADR 0051) setzt
  `fabrikant egress start` voraus; ohne laufenden Proxy scheitern Agent-HTTP-Calls
  (fail-closed, kein Datenabfluss). Zusammen mit dem Proxy erhält der Container
  `NO_PROXY` (`localhost,127.0.0.1,::1`) und die Kleinschreib-Varianten
  `http_proxy`/`https_proxy`/`no_proxy` (curl/wget lesen nur die kleinen):
  Loopback-Verkehr umgeht den Proxy bewusst — er bleibt container-lokal und
  hat keinen Egress-Effekt.
- **Cancel:** `cancel()` ruft zusätzlich `docker rm -f` auf — ein `killpg` auf die
  docker-CLI-Prozessgruppe beendet das laufende Image nicht — und verwirft das Ergebnis.

## Konsequenzen

- Ein kompromittierter Agent kann weder Dateien des Hosts noch `.git` des Host-Repos
  schreiben; was er liefert, ist ein geprüfter Diff im Worktree. Egress ist auf die
  Allowlist beschränkt.
- Eigene Commits des Agenten werden zu Worktree-Änderungen zusammengefasst (Commit-Messages
  gehen verloren); committet wird wie bisher durch fabrikant.
- Jeder Agent-Step klont den Stand neu: Abhängigkeits-Caches im Container sind kalt
  (langsameres `uv sync` o. Ä.); bei großen Repos kostet das Bundle Zeit und Platz.
- Steps mit `github_identity:` geben dem Container weiterhin das Token ihrer Rolle (bewusste
  Übergabe, ADR 0050) — der Agent erreicht `github.com` über den Proxy.
- Docker wird für den Agent-Betrieb erforderlich (bisher nur optional). Das
  Sandbox-Image (`FABRIKANT_SANDBOX_IMAGE`, Default `fabrikant/agent-sandbox:latest`)
  muss die Agent-CLI und git enthalten — Image-Build ist ein Deployment-Pflichtteil.
- `FABRIKANT_SANDBOX` bleibt opt-in (Default aus).

## Restrisiko

- **Gemeinsamer Kernel:** Container-Isolation ohne userns-remap/gVisor; ein Kernel-Escape
  bleibt möglich.
- **Das Ergebnis ist der Einstieg in die Host-Ausführung:** Makefile, `conftest.py`, Hooks,
  `.github/workflows/*` aus dem übernommenen Diff laufen später mit Host-Rechten bzw. in CI
  (siehe „Offene Punkte“). Änderungen an solchen Dateien gehören in die menschliche Review.
- **Laterale Erreichbarkeit (L2):** Parallele Runs teilen das Netz `fabrikant-sandbox` und können
  einander seitlich erreichen; keine Isolation zwischen Runs desselben Users.
- **Plattenbedarf auf dem Host:** `/out` ist ein Bind-Mount ohne Quota; die Größenprüfung greift
  erst nach dem Lauf. Auf einem eigenen Dateisystem/Quota betreiben.
- **Das Bundle enthält die gesamte Historie** (auch längst gelöschte Dateien/Secrets in alten
  Commits). Für sensible Repos ein flaches Bundle als Follow-up.

## Offene Punkte (Grund für „In Überarbeitung“)

- **Sandbox-Image:** `Dockerfile` + `fabrikant sandbox build-image` fehlen noch; ohne Image ist
  die Sandbox nicht nutzbar.
- **Repo-Code läuft weiter auf dem Host:** Lint- und Test-Steps nach dem Agenten führen Code
  aus dem Repo auf dem Host aus (`make lint`, `conftest.py`, Build-Skripte) — auch das, was
  der Agent geschrieben hat. Eine dichte Grenze braucht diese Steps ebenfalls in der Sandbox.
- **Caches:** ein Cache-Volume pro Projekt (nicht das Host-Home) gegen kalte Caches.
- **Mehrere Agent-Steps pro Run** (z. B. `pr-reply`): funktionieren (jeder Step klont den
  aktuellen Worktree-Stand), kosten aber pro Step einen Austausch.
