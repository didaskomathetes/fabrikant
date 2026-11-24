# ADR 0024: Review als GitHub-Review mit Verdikt-Marker, Inline-Threads, Reply-Sperre

**Status:** Akzeptiert (2026-10-19, Merge #130; vorgeschlagen 2026-10-19)
**Entscheider:** Mensch (Freigabe ausstehend)
**Kontext:** PR #124 hing in einer Review-Schleife. Der PR wurde vom
persönlichen Account erstellt — derselbe Account, mit dem `review-pr` reviewt.
GitHub verbietet Approve **und** Request-Changes auf eigene PRs
(`Can not approve your own pull request`); ADR 0010 löst das nur für
`develop`-PRs (App erstellt, PAT reviewt). Folge: Der Review landete nur als
PR-Kommentar, `reviews` blieb leer, der Merge-Check in `pr-reply` (letzte
Review `APPROVED`) konnte nie greifen. Retries posteten denselben Review doppelt.

Zweitens setzte `pr-reply` auf Thread-Replies (`in_reply_to`) als
Self-Loop-Sperre. PR-Kommentare (Issue-Comments-API) haben keine Threads —
jede Antwort war top-level und galt im nächsten Run als "neu". Nur das
LLM-Urteil ("klingt nach meiner eigenen Antwort") verhinderte die Endlosschleife.

Drittens konnte der Reviewer nur einen Block Prosa schreiben — konkrete,
zeilengenaue Hinweise und übernehmbare Suggestions waren nicht möglich.

## Entscheidung

**Ein Review pro Head-SHA als GitHub-Review-Objekt**, gepostet und ausgewertet
**deterministisch** von `scripts/pr-status.py` (nicht vom Agent):

- **Summary** = Review-Body, letzte Zeile ist der Verdikt-Marker
  `<!-- fabrik-review verdict=approved|changes_requested sha=<headRefOid> -->`.
- **Inline-Kommentare** (optional, nach Urteil des Reviewers, max. 10) im selben
  Review, gern mit ```` ```suggestion ````-Blöcken.
- `post-review`: Event `COMMENT` auf eigenen PRs, sonst `APPROVE` /
  `REQUEST_CHANGES` aus dem Marker; `commit_id` = Marker-SHA; idempotent pro
  SHA (Retry-Schutz); lehnt GitHub Inline-Zeilen ab (HTTP 422), wird ohne
  Inline gepostet und die Anmerkungen wandern in die Summary.
- `merge-check`: Freigabe = jüngster Marker des Reviewer-Accounts (`gh api user`)
  ist `approved` **für den aktuellen Head-SHA**, sonst eine GitHub-Review
  `APPROVED` ohne Marker (Mensch, `pr_review`-Step). Gesperrt durch
  `CHANGES_REQUESTED` ohne Marker und durch **offene Review-Threads**.
- `pr-reply` antwortet auf Inline-Kommentare im Thread, übernimmt passende
  Suggestions und löst den Thread auf (`resolve-thread`) nur, wenn das
  Anliegen an der Zeile vollständig umgesetzt ist. Abgelehnte oder nur
  teilweise umgesetzte Punkte bleiben offen und blockieren den Merge, bis ein
  Mensch entscheidet.
- `<!-- fabrik-reply -->` — letzte Zeile jeder `pr-reply`-Antwort;
  `new-comments` liefert solche Kommentare nie als neu. Quellen: PR-Kommentare,
  Review-Summaries, Inline-Kommentare (State-Keys `<num>-<id>`,
  `<num>-review-<id>`, `<num>-inline-<id>`).

## Begründung

- `event: COMMENT` ist auf eigenen PRs erlaubt — ein Mechanismus für Bot- und
  Mensch-PRs, ohne neue Identität.
- SHA-Bindung: Nach neuen Commits ist die alte Freigabe ungültig — Re-Review
  ist Pflicht, wie bei "dismiss stale reviews".
- Inline-Threads haben echte Threads und einen Resolved-Status — das ist der
  natürliche Ort für "erledigt?" statt LLM-Urteil über Kommentarketten.
- Script statt Prompt-jq: testbar (`tests/test_pr_status.py`), kein LLM-Raten
  über Roh-JSON, keine Doppel-Posts.

## Abgelehnte Alternativen

- **Zweite Identität für Mensch-PRs** (App approvet): sauberste GitHub-Semantik,
  aber die App bräuchte Review-Rechte und ADR 0010 (App = Developer) würde aufgeweicht.
- **Verdikt als PR-Kommentar + separate Inline-Review**: zwei Posts,
  zwei Notifications, nicht atomar.
- **`reviewDecision`**: bleibt bei eigenen PRs leer.
- **Autor-Filter für Replies**: Reviewer und Developer teilen sich bei
  Mensch-PRs den Account — nicht unterscheidbar.

## Folgen

- Mensch-PRs mit `fabrik`-Label werden nach LLM-Freigabe (Marker, keine offenen
  Threads) automatisch gemergt — wie Bot-PRs. Wer das nicht will, setzt kein
  `fabrik`-Label.
- Grenze: `pr-reply` nutzt denselben Account wie der Reviewer und *könnte*
  einen Review-Marker schreiben oder Threads ungerechtfertigt auflösen. Der
  Prompt verbietet Ersteres; Kommentare sind untrusted. Echte Trennung bräuchte
  getrennte Tokens für die beiden Loops.
- Inline-Zeilen muss das Modell aus dem Diff korrekt ableiten; Fehler kosten
  nur die Zeilenbindung (422-Fallback), nicht den Review.
- Bestehende Antworten ohne Marker stehen im State (`replied-comments.json`)
  und bleiben ausgeblendet.
