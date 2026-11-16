# ADR 0038: Live-Log und Transkript im Run-Modal (SSE)

**Status:** Vorgeschlagen (2026-10-23)
**Baut auf:** ADR 0002 (SessionLog), ADR 0030 (Auth/Rollen).

## Kontext

Das Run-Modal lud das Log einmalig und pollte laufende Runs; Prompts, Tool-Calls und Antworten
der Agent-Session waren in der UI nicht sichtbar.

## Entscheidung

1. **`GET /api/run/<id>/stream`** liefert Server-Sent Events (Stdlib, ein Thread pro Verbindung via
   `ThreadingHTTPServer`). Events: `log` (`id` = Byte-Offset hinter der Zeile), `status`, `end`;
   `: keepalive` alle 15 s. Quelle ist die Log-Datei, die der Server alle 0,5 s tail-artig liest —
   kein neuer Kanal zwischen Engine und Server.
2. **Reconnect** über `Last-Event-ID` (bzw. `?offset=`): Fortsetzung exakt am Offset, keine Duplikate.
   Es werden nur vollständige Zeilen gesendet; der Rest wird erst bei Run-Ende geflusht.
3. **Redaction:** jeder `log`-Payload läuft durch `secrets._redact`. Dank Zeilen-Chunking kann ein
   Secret nicht an einer Chunk-Grenze durchrutschen.
4. **Transkript:** `GET /api/run/<id>/transcript?after=&limit=` liest Agent-Turns (`prompt`,
   `tool_call`, `message`) aus dem SessionLog (seitenweise, `after` = Event-id). Neu: der Agent-Step
   schreibt den Prompt als `prompt`-Event.
5. **UI:** Tabs Übersicht · Log (Live, „folgen“-Toggle) · Transkript. Ohne `EventSource` oder bei
   dauerhaftem Verbindungsfehler fällt das Log-Tab auf Polling von `/log` zurück.

## Konsequenzen

- Jeder offene Stream hält einen Server-Thread (Homelab-Maßstab akzeptabel); `needs_approval`-Runs
  bleiben offen, bis der Client schließt.
- Schon vor diesem ADR geschriebene Sessions haben kein `prompt`-Event.
