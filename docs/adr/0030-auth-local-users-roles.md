# ADR 0030: Authentifizierung + Autorisierung (lokale Benutzer, Rollen)

**Status:** Akzeptiert (2026-10-21, Merge #248; vorgeschlagen 2026-10-20)
**Betrifft:** README-Prinzip „lokal, 127.0.0.1“, Sicherheitsannahmen (Issue #159). Ergänzt ADR 0019 (Rollen).

## Kontext

Bisher schützt nur ein optionales geteiltes Bearer-Token (`FABRIK_TOKEN`) plus Origin-Check die
`POST`-Routes; `GET` (Runs, Logs, Specs) ist offen. Für einen geteilten Server fehlen Benutzer,
Rollen und ein Audit, wer was gestartet/freigegeben hat.

## Entscheidung

- **Modi** (`FABRIK_AUTH`): `none` (Default, rückwärtskompatibel: GET offen, POST wie bisher
  Origin + optionales Token), `local` (Benutzer + Rollen). `oidc` ist Folge-Issue #160; der Wert
  wird bis dahin abgelehnt.
- **Bind:** Default `127.0.0.1` (`--host` / `FABRIK_HOST`). Ein Nicht-Loopback-Bind mit `none`
  wird beim Start verweigert (`auth.check_bind`). Kein TLS im Prozess: Betrieb über Reverse-Proxy,
  dort `FABRIK_COOKIE_SECURE=1` setzen.
- **Benutzer:** `state/users.json` (0600), Passwort als `hashlib.scrypt` (n=2^14, r=8, p=1,
  16-Byte-Salt, Format `scrypt$n$r$p$salt$hash`), Mindestlänge 8. Verwaltung per CLI
  `fabrik user add|passwd|role`. Unbekannter Benutzer kostet dieselbe Hash-Zeit (kein
  Benutzer-Orakel). Kein Rate-Limit/Lockout in diesem Schritt (Folge-Issue, Reverse-Proxy).
- **Sessions:** serverseitig im Speicher (gehen beim Restart verloren), 256-Bit-Zufalls-Id,
  absolute Lebensdauer `FABRIK_SESSION_TTL` (Default 8 h). Cookie `fabrik_session`:
  `HttpOnly; SameSite=Strict; Path=/`. Die Rolle wird pro Request aus dem Store gelesen —
  Rollenwechsel/Löschen wirken sofort; Passwortänderung beendet die Sessions (im selben Prozess).
- **CSRF:** Jede Session hat ein Token (Antwort von `/api/login`, `/api/session`). Schreibende
  Requests mit Session-Auth brauchen den Header `X-CSRF-Token`; zusätzlich bleibt der Origin-Check.
  Token-Aufrufer (kein Cookie) sind davon ausgenommen.
- **Rollen** (aufsteigend): `viewer` (alle `GET`), `operator` (+ Runs starten, approve/reject,
  pausieren, Chat), `admin` (+ LLM-Provider, Branch-Wechsel, Spec-`apply` im Chat; Benutzer
  verwalten per CLI). Pro-Projekt-Rollen sind bewusst nicht Teil dieses Schritts.
  Öffentlich bleiben nur die UI-Dateien, `/api/health`, `/api/login`, `/api/session`
  und der Webhook (eigene HMAC-Auth, ADR 0027).
- **API-Token:** `FABRIK_TOKEN` gilt in beiden Modi als `admin` (Name `token`) für CLI/Automation.
  Tokens pro Benutzer: später.
- **Audit:** Der Run-Record erhält `started_by`, `approved_by`, `rejected_by` (Benutzername, `token`,
  `cli:<os-user>` oder `webhook`; leer bei Trigger-Runs und Modus `none` ohne Token).
  Pause/Resume-Audit nennt `user:<name>`.

## Konsequenzen

- Modus `none` ändert nichts am bestehenden Verhalten.
- Shared-Server-Betrieb ist möglich, aber ohne TLS/Rate-Limit nur hinter Reverse-Proxy sinnvoll.
- Neue Routes müssen in `auth.required_role` passen (Default: schreibend = `operator`).
