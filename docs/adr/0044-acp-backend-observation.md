# ADR 0044: ACP-Backend (OpenHands via SDK/ACP) — beobachten, nicht adoptieren

**Status:** Akzeptiert (2026-11-13)
**Bezug:** `docs/framework-v1-draft.md` Phase 3 ("ACP-Backend (wenn Standard)"), `docs/backends.md`.

## Kontext

Das Framework-Draft sieht ein ACP-Backend (Agent Client Protocol; z. B. OpenHands über
SDK/ACP) als optionalen Worker-Standard vor — aber erst, wenn ACP sich durchgesetzt hat.
Fabrik hat heute pluggable Backends (`opencode`, `claude-code`, `codex`, `copilot`,
oneshot; siehe `docs/backends.md`), die den Bedarf abdecken.

## Entscheidung

- **Jetzt:** kein ACP-Code, keine Dependency, kein Platzhalter-Backend. Status:
  **Beobachten**. Der aktuelle Stand wird bei jedem Review dieser ADR neu bewertet.
- **Adoption** (neuer Backend-Adapter, eigene ADR + Issue) erst, wenn **alle** Kriterien
  erfüllt sind:
  1. **Stabilität:** ACP-Spec in einer als stabil markierten Version (kein Preview/0.x
     mit Breaking Changes in den letzten 6 Monaten).
  2. **Verbreitung:** mindestens zwei der von Fabrik genutzten Agents (opencode,
     Claude Code, Codex, Copilot) sprechen ACP nativ oder über einen gepflegten Adapter.
  3. **Gleichwertigkeit:** Ein ACP-Client kann die Backend-Schnittstelle
     (`docs/backends.md`: Session, Prompt, Polling/Streaming, Abbruch, Permissions,
     Token-Usage) ohne Fabrik-Sonderpfade erfüllen; Permission-Requests lassen sich
     auf den Approval-Flow (ADR 0011) abbilden.
  4. **Mehrwert:** Es ersetzt mehr Backend-Code, als es hinzufügt (z. B. ein Adapter
     statt N Backend-Module) oder erschließt einen Agent, den Fabrik sonst nicht
     anbinden kann.
  5. **Betrieb:** Ein Referenz-Client ist wartbar (aktive Releases, Lizenz kompatibel)
     und läuft in CI ohne externe Dienste (Fake-Server).
- **Beobachtungsstand (2026-10-23):** Kriterien 1–5 nicht belegt/nicht geprüft;
  Adoption nicht empfohlen. Re-Review: bei Release-Ankündigungen der genannten Agents,
  spätestens quartalsweise.

## Konsequenzen

Keine Code-Änderung. Die Entscheidung ist umkehrbar; das Backend-Interface bleibt die
Adapter-Grenze, ein späteres ACP-Backend wäre ein weiteres Modul dahinter.
