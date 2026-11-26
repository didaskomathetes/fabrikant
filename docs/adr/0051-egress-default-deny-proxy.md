# ADR 0051: Egress default-deny — Agent-Internetzugang nur über den Allowlist-Proxy

**Status:** Akzeptiert (2026-11-19) — umgesetzt (`egress.py`, `fabrikant egress`, Denied-Log-Parser)
**Betrifft:** Security (Prompt-Injection-Datenabfluss, OWASP ASI01/ASI05), Agent-Container, Deployment.
**Issue:** #519.

## Kontext

Agent-Prozesse können frei ins Internet: Eine erfolgreiche Prompt-Injection kann Daten
exfiltrieren (HTTP-/DNS-Tunneling), arbitrary APIs aufrufen oder Cloud-Metadaten lesen
(`169.254.169.254` — z. B. IAM-Roles auf einer Cloud-VM). Prompt-basierte Defense
reicht nicht (OWASP: Injection ist nicht perfekt veränderbar) — die Kontrolle muss in
der Plattform sitzen. ADR 0050 (Env-Allowlist) begrenzt, *was* zu stehlen ist; dieses
ADR begrenzt, *wohin* es gehen kann.

## Entscheidung

- **Netzwerktopologie (Docker):** Zwei Netzwerke — `fabrikant-egress` (Bridge, mit
  Internet; Proxy-Container lebt hier) und `fabrikant-sandbox` (`internal` — kein direkter
  Internetzugang; Agent-Container hängen hier). `ensure_networks` legt sie an **und prüft
  vorhandene** (`internal` und Subnetz müssen passen, sonst Fehler — ein fremdes, nicht
  internes `fabrikant-sandbox` wäre eine stille Isolationslücke). Subnets sind GEPINNT
  (`--subnet 172.27.0.0/16` / `--subnet 172.28.0.0/16`, per
  `FABRIKANT_EGRESS_SUBNET`/`FABRIKANT_SANDBOX_SUBNET` überschreibbar) — die gerenderten
  `Allow`-Zeilen referenzieren genau diese Werte.
- **Proxy:** Tinyproxy-Container (`fabrikant-egress-proxy`), Image **per Digest gepinnt**
  (`vimagick/tinyproxy`, tinyproxy 1.11.0 — Config-Direktiven sind versionsabhängig; ein
  Update erfordert einen Test mit echtem Proxy). Gehärtet wie die Agent-Container
  (`--cap-drop ALL`, `no-new-privileges`, `--read-only`, Speicher-/PID-Limit) und läuft als
  Host-uid:gid, damit er Config/Filter/Log als Eigentümer liest und schreibt (als root ohne
  `CAP_DAC_OVERRIDE` sperrte ein unlesbarer Filter still ALLES und das Log blieb leer).
  **Rootless Docker** (`docker info` meldet `name=rootless`): dort läuft er als Container-`0:0`.
  Im User-Namespace des rootless Daemons *ist* Container-root der Host-User; die Host-uid im
  Container landete dagegen auf einer subuid ohne Zugriff auf die `0600`-Config (Proxy startet
  nicht). Ohne Capabilities hat dieser root keine Sonderrechte (`egress.proxy_user`).
  Config + Filter-Datei werden
  von fabrikant gerendert (`egress.render_proxy_config`/`render_filter_file`) und
  read-only gemountet. Default-Deny = `FilterDefaultDeny Yes` + Filter-DATEI
  (`Filter` in der Tinyproxy-Config ist ein Dateipfad, keine Inline-Patterns; ohne
  `FilterDefaultDeny` würde Tinyproxy ALLES erlauben). Filter-Muster sind ankernde
  POSIX-EREs (`FilterExtended Yes`; `^([a-z0-9-]+\.)*github\.com$`) — exakt + Subdomänen, aber
  `github.com.attacker.net` matcht NICHT. `Allow` beschränkt die Clients auf die
  gepinnten Bridge-Subnets (Agenten im Sandbox-Netz; Host-Clienten via published Port
  kommen aus dem Egress-Bridge-Netz, nicht aus 127.0.0.1).
- **Allowlist:** Builtins (`github.com` — inkl. `api.github.com` als Subdomäne,
  `models.dev`, `api.anthropic.com`, `api.openai.com`) + optionale Datei
  (`FABRIKANT_EGRESS_ALLOWLIST_FILE`, Vorlage `deploy/egress-allowlist.txt`) + Env
  (`FABRIKANT_EGRESS_ALLOWLIST`, kommasetrennt). Jeder Eintrag wird validiert (Hostname mit
  mind. zwei Labels; `com`, IP-Literale, Pfade, Klammern werden abgelehnt) — fail-closed beim
  Start. `169.254.169.254` ist nicht in der Allowlist → unerreichbar.
- **CLI:** `fabrikant egress start|stop|status`. Start legt die Netzwerke idempotent an,
  schreibt Config + Filter-Datei (0600 nach `state/egress/`) und setzt den Container. Danach
  prüft ein Health-Check (Container läuft + Proxy-Port antwortet, `egress.READY_TIMEOUT`);
  sonst Fehler mit Container-Log und der Container wird entfernt (kein stiller toter Proxy).
- **Denied-Requests als Injection-Signal:** Tinyproxy loggt Denials als
  `Proxying refused on filtered domain "<ziel>"`; die Quell-IP steht in der zugehörigen
  `Connect`-Zeile (Zuordnung best effort über den File-Descriptor).
  `egress.parse_denied`/`collect_denied` (Byte-Offset, nur vollständige Zeilen, Dedup je
  Abgleich) machen sie auswertbar; ein nicht lesbares Log ist ein Fehler, kein Schweigen.
  Das Ziel stammt vom Agent und ist **untrusted** (Steuerzeichen entfernt, Länge begrenzt) —
  vor Alert-/Triage-Prompts weiter behandeln. Anbindung an die Alert-Pipeline (ADR 0025/0026)
  folgt als Follow-up.

## Konsequenzen

- Datenabfluss aus dem Agent-Container ist auf die Allowlist begrenzt. DNS-Tunneling: auf
  `--internal`-Netzen scheiterte die Auflösung externer Namen mit Docker 29.9.2 (SERVFAIL);
  das ist **Docker-versionsabhängig** und beim Deployment zu prüfen.
- Agent-Tools, die andere Domänen brauchen (z. B. ein Model-Provider), müssen in die
  Allowlist — eine bewusste, auditierte Entscheidung statt impliziter Erreichbarkeit.
- Docker ist ab jetzt ein Fabrikant-Bedarf im Agent-Betrieb (bisher nur optional für
  SonarQube). Der Proxy selbst ist trivial klein (Memory-Default des Containers).

## Restrisiko

- **Geteilter `opencode serve`-Prozess ist nicht geschützt:** Er läuft unsandboxed auf dem
  Host mit vollem Netzwerkzugang und ist das primäre Agent-Backend. Der Proxy begrenzt nur
  Agenten in `fabrikant-sandbox` (One-Shot-Backends in der Per-Run-Sandbox, Issue #520); der
  Egress von `opencode serve` bleibt ohne Deployment-Änderung offen.
- **Erlaubte Domänen sind Kanäle:** `github.com` samt Subdomänen (Gist, API, Uploads) erlaubt mit
  dem Token des Steps Schreibzugriffe im Umfang dieses Tokens; die Anbieter-APIs
  (`api.anthropic.com`, `api.openai.com`) nehmen beliebigen Text entgegen. Die Allowlist
  begrenzt das Ziel, nicht den Inhalt. Token-Umfang klein halten (ADR 0050).
- **Geteiltes Sandbox-Netz:** Gleichzeitige Agent-Container im selben `fabrikant-sandbox` sehen
  einander (Inter-Container-Verkehr ist an; ihn zu sperren würde auch den Weg zum Proxy
  sperren). Ein Netz pro Run ist ein möglicher Follow-up.
- **Der Proxy** ist ein Single Point of Failure und sicherheitskritisch: Image-Updates nur per
  Digest und mit Test gegen den echten Proxy.
