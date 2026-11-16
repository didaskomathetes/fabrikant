# ADR 0040: `on_slack`-Trigger (Polling, Bot-Token, Whitelist)

**Status:** Akzeptiert (2026-11-13, Merge #324; vorgeschlagen 2026-10-23).
**Ergänzt:** ADR 0001 (Cron + Agent + State), ADR 0027 (`on_webhook`).

## Kontext

Alerts landen in Slack (`#alerts` oder DM an den Bot). Daraus soll ohne manuellen
Anstoß ein Triage-Run starten. Slack bietet Socket Mode (WebSocket), Events API
(öffentlicher HTTP-Endpoint) und `conversations.history` (Pull).

## Entscheidung

- **Verfahren: Polling von `conversations.history`** mit Bot-Token (Scheduler,
  Intervall `poll`, Default 60 s) — dasselbe Muster wie `on_issue`/`on_pr`.
  *Verworfen:* Events API (bräuchte Tunnel + Signaturprüfung, vergrößert die
  Angriffsfläche aus ADR 0027) und Socket Mode (dauerhafte Verbindung + neue
  Abhängigkeit im Scheduler-Thread). Beides ist später als zusätzliche Quelle
  hinter `SlackClient` möglich; Dedup und Run-Start bleiben gleich.
- **Spec:** `type: on_slack`, `token_env` (Env-Name), `channels` (Whitelist von
  Channel-IDs, auch DM-IDs), optional `pattern` (Regex-Suche im Text; ohne =
  jede Nachricht), `poll`. Nicht gelistete Channels werden nie gelesen.
- **Nachrichten:** nur normale und `bot_message`-Nachrichten mit Text; Edits,
  Joins, Deletes (andere `subtype`) zählen nicht.
- **Dedup (State):** `state/slack/<projekt>__<loop>.json` hält pro Channel
  `cursor` (höchster gesehener `ts`) und `seen` (gestartete `ts`, max. 500).
  Erster Poll setzt nur den Cursor (kein Backlog-Run). Doppelzustellung
  desselben `ts` → genau 1 Run.
- **Ein Run-Pfad:** Der Scheduler ruft `_safe_run` mit Trigger
  `on_slack:<channel>:<ts>` und Initial-Variablen `alert_text`, `alert_channel`,
  `alert_ts` auf — dieselbe Start-Funktion wie `on_issue`.
- **Secrets:** Token nur aus der Env-Variable `token_env`; ein `token`-Feld im
  Spec wird abgelehnt. Token steht nicht in State; Fehler-Logs enthalten nur den
  Slack-Fehlercode bzw. Exception-Typ. Benötigte Scopes: `channels:history`,
  `groups:history`, `im:history`.
- **Untrusted:** `alert_text` ist externer Content (framework §1.4).
- **Limits (optional, Defaults in `config`):** `max_pages` (Default 20) begrenzt die
  `conversations.history`-Seiten pro Channel und Poll. Slack liefert neueste zuerst;
  bei erreichtem Limit werden die gelieferten Nachrichten verarbeitet, der `cursor`
  bleibt stehen und die Lücke (`gap_latest`/`gap_high`) wird in Folge-Polls über
  `latest` nachgeholt — kein Alert geht verloren, die Reihenfolge über Polls ist dann
  nicht chronologisch. `max_alert_chars` (Default 4000) kürzt `alert_text` mit Marker
  `… [gekürzt, N Zeichen]`; `pattern` prüft den vollen Text.

## Konsequenzen

Latenz bis zum Poll-Intervall; keine zusätzliche Infrastruktur. Ein Run, der
beim Start verworfen wird (Limit/Lock), bleibt als nicht gestarteter Alert
pending und wird beim nächsten Poll erneut versucht. Dedup überlebt Restarts
durch die State-Datei.

## Trigger-Matrix

| Trigger | Quelle | Mechanismus | Auth | Dedup |
|---|---|---|---|---|
| `on_issue` | GitHub-Issues | Polling | GitHub-Token | `seen`-Set / Run-Records (reconcile) |
| `on_webhook` | beliebiger HTTP-Sender | Push (`POST /api/webhook/…`) | HMAC-SHA256 | keiner (Sender-Sache) |
| `on_slack` | Slack-Channels/DMs | Polling (`conversations.history`) | Bot-Token (Env) | `ts` im State |
