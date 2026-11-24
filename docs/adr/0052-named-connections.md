# ADR 0052: Benannte Verbindungen — LLM-Provider und Agent-Backends in der UI konfigurieren

**Status:** Akzeptiert (2026-11-06) — mit dem Merge von PR #508; dazu Warnung in `fabrik validate`
bei unbekannter `connection:` und UI-Hinweis für Secrets unter der Masking-Grenze. Vorgeschlagen 2026-11-04.
**Baut auf:** ADR 0030 (Auth/Rollen), ADR 0034 (SQL-Run-Store), ADR 0049 (UI-i18n). Umsetzung der Settings-Anforderung aus `features.md` (erste Stufe).

## Kontext

LLM-Provider (`FABRIK_LLM_<NAME>_*`) und das Agent-Backend (`FABRIK_AGENT_*`) sind nur per Env-Datei
konfigurierbar; jede Änderung braucht Dateizugriff und Neustart. Credentials (API-Keys, opencode-Passwort)
liegen im Klartext in `fabrik.env`. Gewünscht sind **benannte Verbindungen**, die ein Admin in einem
Settings-Menü der UI pflegt und die Steps beim Namen referenzieren.

Erste Stufe: LLM-Provider und Agent-Backends. GitHub, GitHub-Apps und SonarQube folgen mit demselben
Modell (eigene `kind`-Werte), sobald der Token-Refresh-Ablauf (ADR 0010) dafür angepasst ist.

## Entscheidung

1. **Datenmodell:** Tabelle `connections` im SQL-Store (Alembic-Revision 0002, unabhängig vom
   Run-Store-Backend immer über `FABRIK_DB_URL`): `name` (PK), `kind` (`llm` | `agent`), `driver`
   (Agent: Backend-Name; LLM: `http` oder CLI-Name), `endpoint`, `model`, `username`, `secret`,
   `is_default`. Ein Default je Art.
2. **Secrets verschlüsselt:** `secret` ist ein Fernet-Token (`cryptography`). Schlüssel aus
   `FABRIK_SECRET_KEY`, sonst `state/secret.key` (beim ersten Bedarf mit Modus 0600 erzeugt). Die API
   gibt Secrets nie aus (`secret_set`); beim Speichern behält ein fehlendes `secret` den alten Wert,
   `clear_secret` löscht ihn. Entschlüsselte Werte werden beim Log-/Record-Masking
   (`secrets.register_source`) mitmaskiert. Ist der Schlüssel verloren, ist das Secret leer
   (Warnung im Log) — der Admin trägt es neu ein.
3. **Env bleibt gültig:** Env-Provider und `FABRIK_AGENT_*` erscheinen als schreibgeschützte
   Verbindungen (`source: env`; Agent-Verbindung `env`). Bei Namensgleichheit gewinnt die UI-Verbindung
   („Überschreiben“ in der UI). Keine Migration, kein Breaking Change.
4. **Auflösung im Step:** `connections.apply_to_step` ergänzt vor dem Dispatch (`engine._dispatch_step`)
   `backend/endpoint/model/username/password` eines `agent`-/`review`-/`pr_review`-Steps. Rangfolge:
   **Step-Felder > `connection:` im Step > Default-Verbindung > Env.** Die Default-Verbindung greift nur,
   wenn das Step-`backend` fehlt oder zu ihrem Treiber passt; ein explizites `connection:` mit
   abweichendem `backend` oder unbekanntem Namen ist ein Environment-Fehler. Die Backends bleiben
   unverändert. LLM-Verbindungen fließen über `llm.list_providers()` in die bestehende Provider-Auswahl
   (`provider:` im Step, aktiver Provider im Chat).
5. **API und Rechte:** `GET /api/connections` (viewer; Liste ohne Secrets + wählbare Treiber),
   `PUT`/`DELETE /api/connections/<name>` (admin). Schreibzugriffe laufen durch die bestehende
   Origin-/CSRF-Prüfung.
6. **UI:** ⚙-Button im Header öffnet den Dialog „Einstellungen · Verbindungen“ (Liste, Anlegen,
   Bearbeiten, Löschen; i18n de/en).

## Nicht Teil dieser Entscheidung

- Verbindungstest („Test“-Button), Rotation des `FABRIK_SECRET_KEY`, GitHub/SonarQube-Verbindungen.
- Per-Projekt-Zuordnung von Verbindungen (ein Step wählt per Name).

## Konsequenzen

- Neue Abhängigkeit `cryptography`.
- Die DB-Datei enthält verschlüsselte Secrets; Schlüssel und DB gehören nicht in dasselbe Backup ohne Schutz.
- Mehrere fabrik-Prozesse auf derselben DB sehen Änderungen des Masking-Caches erst nach Neustart.
- `pr_review`-Fix-Runden übernehmen Backend/Modell des Builders (ADR 0010), nicht die Verbindung des Reviewers.
