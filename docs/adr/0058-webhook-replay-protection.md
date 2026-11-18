# ADR 0058: Webhook-Replay-Schutz (opt-in `replay_protection`)

**Status:** Akzeptiert (2026-11-18)
**Betrifft:** ADR 0027 (`on_webhook`-Trigger) — erweitert dessen „kein Replay-Schutz“ um einen opt-in Modus

## Kontext

ADR 0027 definiert die Webhook-Authentifizierung als HMAC-SHA256 über den rohen
Body (`X-Fabrik-Signature`). Damit ist ein Request mit gültiger Signatur beliebig
oft wiederholbar (Replay): ein abgehörter Request startet denselben Run immer
wieder, solange das Secret gilt. ADR 0027 hatte Replay-Schutz bewusst
ausgeklammert, weil er für alle Trigger Pflicht-Header und eine neue
Signatur-Form bedeutet hätte und damit inkompatibel mit bestehenden Aufrfern
gewesen wäre.

## Entscheidung

Replay-Schutz ist **opt-in pro Trigger** und ändert nichts am bestehenden
Verhalten:

1. **Trigger-Flag.** `replay_protection: true` im `on_webhook`-Trigger aktiviert
   den Modus. Ohne Flag bleibt die ADR-0027-Signatur (HMAC über den Body)
   gültig — abwärtskompatibel.
2. **Pflicht-Header.** Im Modus sind `X-Fabrik-Timestamp` (Unix-Epoch, Sekunden)
   und `X-Fabrik-Nonce` (frei wählbar, max. 128 Zeichen) Pflicht.
3. **Signatur.** Die Signatur deckt `<timestamp>.<nonce>.<body>` ab
   (`sign_replay`), nicht nur den Body. Timestamp und Nonce sind damit
   signiert und nicht nachträglich austauschbar.
4. **Frischheitsfenster.** `|now − timestamp|` darf ±300 s
   (`REPLAY_WINDOW_S`) nicht überschreiten; sonst 401 „Timestamp abgelaufen“.
   Nicht-endliche Timestamps (`nan`, `inf`) sind ungültig (401), weil sie das
   Fenster sonst umgehen würden.
5. **Nonce-Einmaligkeit.** Gesehene Nonces werden in einem thread-sicheren
   In-Prozess-TTL-Cache gemerkt (`NonceCache`, TTL = 2 × Fenster = 600 s);
   Wiederverwendung → 401 „Nonce bereits verwendet“.
6. **Prüfreihenfolge.** Signatur → Fenster → Nonce. Signatur zuerst, damit
   Fehlermeldungen zu Timestamp/Nonce nur für authentische Requests entstehen
   (kein Orakel) und eine falsche Signatur keine Nonce „verbrennt“.

## Konsequenzen

- (+) Replay-Angriffe auf Trigger mit `replay_protection: true` sind innerhalb
  des Fensters und über dessen Nachlauf (TTL) hinaus wirkungslos.
- (+) Bestehende Trigger und Aufrufer sind unverändert (opt-in).
- (−) **Bekannte Einschränkung:** Der Nonce-Cache ist nur im Prozess. Ein
  Server-Neustart innerhalb des Fensters erlaubt genau einen Replay des
  abgehörten Requests (die Nonce ist vergessen). Persistenter Nonce-Store
  (z. B. SQLite/Postgres) wäre die Folge-Ausbaustufe.
- (−) Aufrufer müssen Uhrzeit (±300 s) und Nonce-Generierung mitliefern;
  Trigger mit dem Flag akzeptieren keine Legacy-Signatur mehr.
