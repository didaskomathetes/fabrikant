# ADR 0021: Issue-Priorisierung über Phasen-Labels (`priority_labels`)

**Status:** Akzeptiert (2026-10-20, Merge #213; vorgeschlagen 2026-10-19)
**Kontext:** `refine` und `develop` arbeiten den Issue-Bestand per
`on_issue`-Reconcile ab (ADR 0014), **älteste zuerst**. Die Issues tragen
jetzt Phasen-Labels `p0`–`p3` nach `docs/framework-v1-draft.md` §6. Jede Phase
muss allein nützen und baut auf der vorigen auf. Ein altes p3-Issue
(z. B. Cloud-Deployment) soll daher nicht vor einem neuen p0-Issue
(z. B. Secret-Redaction) refined oder entwickelt werden.

## Entscheidung

- Neue, optionale Trigger-Option `priority_labels` für `on_issue`: eine
  geordnete Label-Liste, höchste Priorität zuerst. Der Rang eines Issues ist
  der Index des **ersten** Labels der Liste, das es trägt. Issues ohne
  Prioritäts-Label kommen zuletzt.
- Innerhalb einer Stufe gilt „älteste zuerst“ (ADR 0014), sortiert nach
  Issue-Nummer, also auch im Event-Modus, der GitHub neueste zuerst abfragt.
- **Reconcile-Modus:** Mit `priority_labels` entfällt der Lazy-Abbruch der
  Pagination. Alle Seiten (≤ `RECONCILE_MAX_PAGES`) werden gelesen, dann
  sortiert, dann auf `max_per_poll` gekürzt. Sonst würde ein p0 auf Seite 2
  hinter älteren unpriorisierten Issues verschwinden.
- **Event-Modus:** Mehrere neue Issues eines Polls feuern in Prioritäts-Reihenfolge.
- Die Labels stehen in der Spec, nicht im Code. In
  `projects/fabrik.fabrik.yaml` nutzen `refine` und `develop`
  `[p0, p1, p2, p3]` (YAML-Anker `*priority_labels`).

## Konsequenzen

- Ohne `priority_labels` ändert sich nichts (abwärtskompatibel).
- Mit Prioritäten liest jeder Reconcile-Poll alle Seiten (≤ 10 GitHub-Requests
  pro Loop und 300 s). Bei der Repo-Größe ist das unkritisch.
- Die Priorisierung gilt **pro Loop**. Zwischen `refine` und `develop` gibt es
  keine Abstimmung; beide konkurrieren weiter um den Projekt-Lock.
- Ein p0-Label, das nach dem Start eines p3-Runs gesetzt wird, unterbricht
  diesen Run nicht. Es wirkt erst beim nächsten Poll.
