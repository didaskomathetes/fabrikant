You are searching for refactoring candidates in the `fabrikant` project (repo `didaskomathetes/fabrikant`) and reporting the best ones as JSON (the `issue` step creates the issues afterwards).

## Procedure
1. Follow `AGENTS.md` (already in your context).
2. Check existing open refactoring issues (no duplicates):
   read the open refactoring issues (labels `fabrikant`,`refactor`) via the GitHub REST API
   (`GET https://api.github.com/repos/didaskomathetes/fabrikant/issues?labels=fabrikant,refactor&state=open`).
3. Read the SonarQube data of the latest main analysis (primary source for code smells +
   test coverage). **Do not start a scan** — the `sonar-scan` loop analyzes main daily.
   - `SONAR_HOST_URL` and `SONAR_TOKEN_FILE` are already in the env. **Do not source env
     files and do not use admin credentials.** Never write the token to the repo, issues,
     commits or logs.
   - Auth: basic auth `<token>:` with the token from `$SONAR_TOKEN_FILE`,
     host `$SONAR_HOST_URL` (default `http://127.0.0.1:9000`), project key `fabrikant:fabrikant`.
   - Open issues (code smells, bugs, vulnerabilities):
     `curl -s -u "$(cat "$SONAR_TOKEN_FILE"):" \
       "$SONAR_HOST_URL/api/issues/search?componentKeys=fabrikant:fabrikant&resolved=false&types=CODE_SMELL,BUG,VULNERABILITY&ps=100"`
   - Test coverage (project + per file):
     `curl -s -u "$(cat "$SONAR_TOKEN_FILE"):" \
       "$SONAR_HOST_URL/api/measures/component?component=fabrikant:fabrikant&metricKeys=coverage,covered_lines,uncovered_lines"`
     and per file:
     `curl -s -u "$(cat "$SONAR_TOKEN_FILE"):" \
       "$SONAR_HOST_URL/api/measures/component_tree?component=fabrikant:fabrikant&metricKeys=coverage"`
   - If SonarQube is unreachable: continue with step 4 without Sonar data and note this in
     the result.
4. Analyze the codebase as a supplement (this directory = the `fabrikant/` package):
   - Complexity: long functions (> ~60 lines), nested if blocks, long modules
   - Duplication: similar code paths in different files
   - Dead code: unused functions/imports/parameters
   - Loose dicts with fixed keys passed through several functions (`d.get("x")` chains) →
     candidate for a dataclass/small class (see `AGENTS.md`)
   - Missing/outdated docs on public functions/classes
   - Coverage weak spots: modules/functions below the 80 % goal from `AGENTS.md`
5. Pick the top candidates: **max. 3** per run. Each candidate needs: concrete file(s) +
   function(s), the problem, an actionable proposal.
   Priority: SonarQube issues (especially code smells) > coverage gaps > manual findings.
6. Do NOT create issues yourself (no `gh`, no API writes). Instead, output a JSON object
   as the last output (no further text after it):
   `{"candidates": [{"title": "<short description>", "body": "<problem>\n\n<proposal>\n\nAffected files:\n- <file>:<function>"}]}`
   (max. 3 entries, `title` WITHOUT prefix — the step sets `refactor(fabrikant):`). For
   SonarQube-based candidates: link the issue key + rule in the body.
7. Before the JSON: briefly report what you found. If there are no sensible candidates:
   "No refactoring candidates found" and `{"candidates": []}` — that is fine.
