# ADR 0056: Deployment-Profil (`homelab|cloud`) mit Adapter-Registry am Composition Root

**Status:** Akzeptiert (2026-11-13)
**Betrifft:** ADR 0034 (SQLite/Postgres-Storage), Folge-Issues #19, #28

## Kontext

Mehrere geplante Issues (#19 Postgres-Registry/Queue, #28 Cloud-Profil) unterscheiden
`homelab` und `cloud`. Ohne zentralen Mechanismus landet das als `if profile == …` oder
Feature-Flag in der Business-Logik.

## Entscheidung

1. **Profil statt Feature-Flags.** Ein Deployment-Profil `homelab|cloud` wird gewählt per
   `--profile` (CLI) > `FABRIK_PROFILE` (Env) > Default `homelab`. Ungültige Werte
   scheitern mit einer Meldung, die die erlaubten Werte nennt.
2. **Adapter-Auswahl am Composition Root.** Eine `AdapterRegistry` bildet `(Profil, Port)` auf
   eine Adapter-Factory ab. Die Auflösung passiert einmal beim Start (`cli.main` →
   `profiles.compose`); die Business-Logik erhält fertige Adapter und kennt den Profilnamen nicht.
3. **Erweiterbar.** Neue Ports (Secrets, Queue, Worker) und Adapter werden registriert, die
   Auswahl-Logik bleibt unverändert. Zunächst nur Port `storage`: `homelab` = lokaler Store
   (Run-JSONs + SQLite-Session-Log), `cloud` = Platzhalter (`NotImplementedError`, Folge-Issues).
4. **Absicherung:** `tests/test_profiles.py` scheitert, wenn ein Profilname oder `Profile` außerhalb
   von `profiles.py` und `cli.py` (Composition Root) im Code auftaucht.

## Konsequenzen

- (+) Profil-Unterschiede sind an einer Stelle sichtbar; Cloud-Adapter kommen ohne Eingriff in die Engine.
- (−) Mit `--profile cloud` bricht jeder Befehl ab, bis ein Cloud-Storage-Adapter existiert (gewollt, fail fast).
- (−) Das Adapter-Objekt (`LocalStorage`) ist vorerst nur Beschreibung der lokalen Pfade; die
  Storage-Schnittstelle selbst definiert ADR 0034 (`RunStore`).
