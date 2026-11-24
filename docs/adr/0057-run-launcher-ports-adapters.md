# ADR 0057: Run-Launcher — Port/Adapter für das Ausführungs-Substrat

**Status:** Akzeptiert (2026-11-18)
**Betrifft:** ADR 0028 (Worktree pro Run), ADR 0043 (Reconcile-Dedup), P3 (ECS/GKE)

## Kontext

Die Engine (`engine.py`) führt Trigger-Runs in einem bounded Worker-Pool
(`FABRIKANT_WORKERS`) und Vorrang-Runs in Daemon-Threads aus. Das
Ausführungs-Substrat ist im Core hart verdrahtet: Scheduling-Policy (Claim,
Dedup, Vorrang, `queued`-Records) und Substrat (Threads) sind vermischt. Das
geplante P3 (ECS/GKE, siehe Modul-Layout in `fabrikant/__init__.py`) braucht
Remote-Ausführung (k8s-Jobs, ECS-Tasks) — ohne Trennung würde das als
`if substrate == …`-Zweige in der Engine landen.

## Entscheidung

1. **Port `RunLauncher` (`fabrikant/launcher/port.py`).** Der Core definiert den
   Vertrag für das Ausführungs-Substrat: `submit` (Fire-and-forget Work),
   `submit_priority` (isoliertes Kontext, darf auf Kapazität warten),
   `status` (Substrat-Beobachtbarkeit als `LauncherStatus`:
   `workers`/`busy`/`queue`) und `shutdown` (Drain). Strukturell
   (Duck-Typing), wie `AgentBackend` — Adapter müssen nicht erben. Die
   Scheduling-Policy bleibt im Core und ist substrat-unabhängig.
2. **In-Prozess-Adapter (`InProcessLauncher`).** Das heutige Verhalten: dünner
   Wrapper um den bestehenden Worker-Pool und Daemon-Threads. Der Pool wird
   vom Core injiziert (`pool_factory`/`pool_or_none`), nicht vom Launcher
   besessen — kein Import-Zyklus, bestehende Tests bleiben gültig.
3. **Registry + `FABRIKANT_LAUNCHER`.** `fabrikant/launcher/registry.py` bildet
   Substrat-Name → Adapter-Factory (analog `fabrikant/backends/registry.py`). Der
   Core importiert nur `create_launcher` und den Port, nie konkrete Adapter.
   Das Substrat wählt `FABRIKANT_LAUNCHER` (Default `inprocess`); unbekannte
   Namen scheitern mit einer Meldung, die die verfügbaren Substrate nennt.
4. **`shutdown()` ist ein vorgriffiger Vertrag.** Die Methode gehört zum Port,
   ist aber noch nicht an das Server-Shutdown angeschlossen — es gibt aktuell
   keinen Drain-Pfad. Sie darf nicht als funktionierende Abschaltroutine
   verstanden werden.

## Konsequenzen

- (+) Neue Substrate (k8s, ECS) werden registriert, ohne die Engine zu ändern.
- (+) `engine.pool_status()` delegiert an den Launcher — die Schedule-View
  meldet den Status des konfigurierten Substrats (eine Quelle, keine Kopie).
- (−) Der Launcher ist ein Modul-Singleton in der Engine; Tests setzen ihn
  über `engine._launcher` zurück.
- (−) `submit_priority` muss Remote-Substrate „Warten auf Kapazität in
  isoliertem Kontext" selbst implementieren — die In-Prozess-Semantik
  (Daemon-Thread) überträgt sich nicht 1:1.
