# ADR 0023: `on_pr` reconcile pro Head-SHA (`reconcile: head`)

- Status: accepted
- Datum: 2026-10-20

## Kontext

`review-pr` nutzt seit #137 den `on_pr`-Trigger im Event-Modus. Zwei
Eigenschaften verlieren dabei Reviews:

1. `_safe_run` verwirft einen Trigger-Run still, wenn der Lock belegt ist
   (`try_acquire_for_new_run`, nicht blockierend). `review-pr` ist ein
   Agent-Loop und teilt den Projekt-Lock mit `develop` (bis 1 h) und `pr-reply`.
2. Der Event-Modus merkt sich `updated_at` als „gesehen“, bevor der Run
   startet — ein verworfener Trigger wird nie nachgeholt. Der Erst-Poll nach
   einem Restart feuert gar nicht.

`reconcile: true` (ADR 0011) ist restart-fest, dedupliziert aber pro PR
(`on_pr:#N`): Jede PR würde genau einmal reviewt, neue Pushes nie.

## Entscheidung

`on_pr` akzeptiert `reconcile: head`. Reconcile wie ADR 0011, aber der
Trigger-Schlüssel enthält die Head-SHA: `on_pr:#N@<sha>`. Ein Poll feuert für
jede passende PR, für deren aktuellen Head noch kein Run-Record existiert.

- Neuer Push → neue SHA → genau ein neuer Run.
- Kommentar/Label ohne Push → gleiche SHA → kein Run.
- Verworfener Trigger (kein Record) → nächster Poll feuert erneut.
- Fehlt `head.sha` in der API-Antwort (theoretisch, die Pulls-API liefert sie
  immer) → Schlüssel `on_pr:#N`; die PR gilt dann als erledigt, sobald
  irgendein Run für sie existiert (`on_pr:#N` oder `on_pr:#N@…`) — kein
  Doppel-Run für einen bereits reviewten Head.
- Neue Variable `{pr_head_sha}`.
- `reconcile: true`/`false` bleiben unverändert; andere Werte sind ein
  Spec-Fehler.

## Konsequenzen

+ Reviews gehen bei Lock-Kontention und Restarts nicht mehr verloren.
+ Opt-in, `approval` (ADR 0011) unverändert.
- Nach Einführung: einmaliger Review aller offenen PRs, deren Head noch
  keinen `@sha`-Record hat (alte Records heißen `cron`/`on_pr:#N`).
- Jeder Poll scannt die Run-Records des Loops (wie ADR 0011/0014). Ein Record
  pro Push statt pro PR lässt die Records schneller wachsen; eine Retention
  für Run-Records fehlt bisher (Follow-up).
- Verwerfen bei belegtem Lock bleibt; nur das Nachholen ist neu. Echte
  Parallelität: Worktree pro Run (#181/#238).
