Du suchst Refactoring-Kandidaten im Projekt `fabrikant` (Repo `didaskomathetes/fabrikant`) und meldet die besten als JSON (die Issues legt anschließend der `issue`-Step an).

## Vorgehen
1. Halte dich an `AGENTS.md` (liegt bereits im Kontext).
2. Bestehende offene Refactoring-Issues prüfen (keine Duplikate):
   Lies die offenen Refactoring-Issues (Label `fabrikant`,`refactor`) über die GitHub-REST-API
   (`GET https://api.github.com/repos/didaskomathetes/fabrikant/issues?labels=fabrikant,refactor&state=open`).
3. SonarQube-Daten der letzten main-Analyse lesen (primäre Quelle für Code Smells +
   Test-Coverage). **Keinen Scan starten** — der `sonar-scan`-Loop analysiert main täglich.
   - `SONAR_HOST_URL` und `SONAR_TOKEN_FILE` stehen bereits im Env. **Keine Env-Dateien
     sourcen und keine Admin-Credentials verwenden.** Den Token nie in Repo, Issues,
     Commits oder Logs schreiben.
   - Auth: Basic-Auth `<token>:` mit dem Token aus `$SONAR_TOKEN_FILE`,
     Host `$SONAR_HOST_URL` (Default `http://127.0.0.1:9000`), Projekt-Key `fabrikant:fabrikant`.
   - Offene Issues (Code Smells, Bugs, Vulnerabilities):
     `curl -s -u "$(cat "$SONAR_TOKEN_FILE"):" \
       "$SONAR_HOST_URL/api/issues/search?componentKeys=fabrikant:fabrikant&resolved=false&types=CODE_SMELL,BUG,VULNERABILITY&ps=100"`
   - Test-Coverage (Projekt + pro Datei):
     `curl -s -u "$(cat "$SONAR_TOKEN_FILE"):" \
       "$SONAR_HOST_URL/api/measures/component?component=fabrikant:fabrikant&metricKeys=coverage,covered_lines,uncovered_lines"`
     und pro Datei:
     `curl -s -u "$(cat "$SONAR_TOKEN_FILE"):" \
       "$SONAR_HOST_URL/api/measures/component_tree?component=fabrikant:fabrikant&metricKeys=coverage"`
   - Ist SonarQube nicht erreichbar: ohne Sonar-Daten weiter mit Schritt 4 und das im
     Ergebnis vermerken.
4. Codebasis ergänzend analysieren (dieses Verzeichnis = das `fabrikant/`-Paket):
   - Komplexität: lange Funktionen (> ~60 Zeilen), verschachtelte if-Blöcke, lange Module
   - Duplikat: ähnliche Code-Pfade in verschiedenen Dateien
   - Toter Code: ungenutzte Funktionen/Imports/Parameter
   - Lose Dicts mit festen Keys, die durch mehrere Funktionen wandern (`d.get("x")`-Ketten) →
     Kandidat für eine Dataclass/kleine Klasse (siehe `AGENTS.md`)
   - Fehlende/veraltete Doku bei öffentlichen Funktionen/Klassen
   - Coverage-Schwachstellen: Module/Funktionen unter dem 80 %-Goal aus `AGENTS.md`
5. Top-Kandidaten auswählen: **max. 3** pro Run. Jeder Kandidat braucht: konkrete Datei(en) +
   Funktion(en), das Problem, einen umsetzbaren Vorschlag.
   Priorität: SonarQube-Issues (v. a. Code Smells) > Coverage-Lücken > manuelle Funde.
6. Erstelle KEINE Issues selbst (kein `gh`, keine API-Writes). Gib stattdessen als letzte
   Ausgabe ein JSON-Objekt aus (ohne weiteren Text danach):
   `{"candidates": [{"title": "<kurze Beschreibung>", "body": "<Problem>\n\n<Vorschlag>\n\nBetroffene Dateien:\n- <Datei>:<Funktion>"}]}`
   (max. 3 Einträge, `title` OHNE Präfix — `refactor(fabrikant):` setzt der Step). Bei
   SonarQube-basierten Kandidaten: Issue-Key + Rule im Body verlinken.
7. Vor dem JSON: kurz reporten, was du gefunden hast. Falls keine sinnvollen Kandidaten:
   "Keine Refactoring-Kandidaten gefunden" und `{"candidates": []}` — das ist ok.
