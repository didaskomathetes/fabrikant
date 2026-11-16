# ADR 0027: `on_webhook`-Trigger (HMAC, Tunnel-CORS)

**Status:** Akzeptiert (2026-10-20, Merge #189; vorgeschlagen 2026-10-20; geändert durch ADR 0028)
**Ergänzt:** `docs/framework-v1-draft.md` §1.3 (aus P0 gestrichen, hier P2).

## Kontext

Der Server bindet auf 127.0.0.1 und prüft nur Origin/optionales Bearer-Token. Ein
Tunnel (cloudflared) würde ihn öffentlich machen: `POST /api/runs` ohne
`Origin`-Header (curl) passiert den Origin-Check und startet Shell-Steps.

## Entscheidung

- **Route:** `POST /api/webhook/<projekt>/<loop>`; Loop braucht einen
  `on_webhook`-Trigger (`secret_env`, optional `map`).
- **Auth:** HMAC-SHA256 über den rohen Body im Header `X-Fabrik-Signature:
  sha256=<hex>`, Vergleich per `hmac.compare_digest`. Fehlende/falsche Signatur,
  unbekanntes Projekt/Loop, kein Trigger oder ungesetztes Secret → einheitlich
  **401** (kein Existenz-Orakel); es startet nie ein Run. Die Route ist von
  Origin/`FABRIK_TOKEN` ausgenommen (Aufrufer sind Server; HMAC ersetzt sie).
- **Secret-Handhabung:** Das Spec nennt nur den Env-Namen (`secret_env`), nie den
  Wert. Pro Loop ein eigenes Secret. Kein Replay-Schutz (Timestamp/Nonce) in
  diesem Schritt — bewusst ausgeklammert, Folge-Issue falls nötig.
- **Payload→Variablen:** `map: {variable: "punkt.pfad"}` (Dict-Keys, Listen-Index).
  Ungültiges JSON/kein Objekt → 400, fehlendes Feld → 422. Werte sind untrusted
  (§1.4), Nicht-Strings werden als JSON serialisiert. Body max. 1 MB (413).
- **Ein Code-Pfad:** Der Start läuft über `FabrikHandler._start_run` — dieselbe
  Funktion wie `POST /api/runs` ("Run mit Initial-Variablen starten"); Trigger
  `on_webhook`, Antwort mit `run_id`, 409 bei belegtem Projekt-Lock.
- **CORS:** `FABRIK_WEBHOOK_ORIGIN` (z. B. `https://hooks.example.com`). Nur auf der
  Webhook-Route und nur bei exakt gleichem `Origin` wird
  `Access-Control-Allow-Origin` gesetzt (+ Preflight `OPTIONS`). Nie `*`.
- **Tunnel-Exposition:** Requests mit `Host` = Tunnel-Host erreichen ausschließlich
  `/api/webhook/*` (alles andere 404). Der Tunnel muss den Original-Host
  durchreichen (cloudflared-Default). Ohne `FABRIK_WEBHOOK_ORIGIN` gibt es keine
  Sperre und kein CORS; der Operator darf den Tunnel dann nicht öffnen.

## Konsequenzen

Webhooks sind erst nach Setzen von Secret und Origin nutzbar. Ein Secret-Leak
erlaubt Runs für genau diesen Loop (Tier-/Approval-Gates greifen weiter).
