# ADR 0005: opencode-Backend — Dual-Support 1.x/2.x

**Status:** Accepted (2026-10-17)

## Kontext

Das `opencode`-Backend (SPIKE: `docs/spike-opencode.md`) wurde gegen die
`opencode serve`-HTTP-API von **1.19** gebaut. opencode **2.x** hat die API
brechend geändert (verifiziert 2026-10-17 gegen 2.0.32 + OpenAPI-Spec):

- **Auth:** HTTP-Basic-Auth ist Pflicht — der Server generiert ein
  Passwort, wenn `OPENCODE_SERVER_PASSWORD` nicht gesetzt ist (nur im
  Server-Log sichtbar).
- **Pfade:** alles unter `/api/*` (`/session` → `/api/session`,
  `prompt_async` → `prompt`, `abort` → `interrupt`, `/api/health` →
  `/api/info`).
- **Session-Create:** `permission` → `permissions`, Rule-Felder
  `permission/pattern/action` → `action/resource/effect`, Workdir über
  `location: {directory}` im Body (statt `?directory=` pro Request),
  Response in `data`-Wrapper.
- **Prompt:** `{"parts": [...]}` → `{"text": "..."}`.
- **Messages:** paginiert (`{data, cursor}`), flache Shape
  (`type`, `content`, top-level `tokens/cost/finish/error/time`) statt
  `{info, parts}`-Envelope.

Bestehende Deployments laufen auf 1.x; 2.x ist der aktuelle Release-Zweig.

## Entscheidung

**Ein Adapter, zwei Dialekte.** `OpenCodeBackend` erkennt die
Server-Version lazy (gecacht): `GET /api/info` → 2.x, sonst
`GET /api/health` → 1.x. Der Transport + die Shape-Normalisierung sitzen in
einer privaten `_ServeApi`-Klasse (Dialekt `1`/`2`); `poll()` bleibt
versionsoagnostisch, weil 2.x-Messages in die 1.x-Envelope
(`{info, parts}`) normalisiert werden (`_normalize_v2`).

- **Auth:** `FABRIK_AGENT_PASSWORD` (optional `FABRIK_AGENT_USERNAME`,
  Default `opencode`), auch als Step-Override `password:`/`username:`.
  1.x ignoriert den Header harmlos. 2.x ohne Passwort → unhealthy mit
  Hinweis in der Fehlermeldung.
- **Stabil geblieben** (deshalb eine `poll()`-Logik): `finish`-Werte,
  Tool-`state.status` (`completed`/`error`/`running`), `tokens.input/output`,
  `cost` als Number.
- **Nicht** gewählt: Version pinnen (1.x wird nicht mehr entwickelt),
  zwei getrennte Adapter (Duplikat der Statuslogik), Client-SDK
  (Abhängigkeit für 5 Endpunkte).

## Konsequenzen

- Deployment auf 2.x: `OPENCODE_SERVER_PASSWORD` am serve-Prozess setzen
  + `FABRIK_AGENT_PASSWORD` in `fabrik.env` (gleicher Wert).
- 2.x bindet das Workdir an die Session (`location`) — `?directory=` pro
  Request existiert nicht mehr; für fabrik irrelevant (Session pro Step).
- Wenn 1.x-EOL kommt: Dialekt `1` + zugehörige Tests entfernen.
