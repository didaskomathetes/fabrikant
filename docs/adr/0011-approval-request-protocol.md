# ADR 0011: Approval-Request-Protokoll (ADR-PR gestapelt auf Original-PR → Mensch approvet via fabrik → Merge)

**Status:** Accepted (2026-10-18, revidiert 2026-10-19); Amendment accepted 2026-10-24 (PR #302 gemergt): Gate direkt am `pr_merge`-Step statt an einem eigenen `approve`-Step

## Kontext

Die Dual-Identity-Pipeline (ADR 0010) hat einen **Deadlock**, wenn eine Änderung
eine ADR/eine kritische Entscheidung erfordert:

1. Der **Reviewer** (`review-pr`-Loop / `pr_review`-Step) verlangt eine ADR für
   eine Architektur-Änderung.
2. Der **Developer-Agent** (`pr-reply`-Loop / Fix-Runde) weigert sich, die ADR
   anzulegen — AGENTS.md: *"never change specs/ADRs without explicit
   confirmation"*.
3. Es gibt **keinen formellen Approval-Request-Kanal** für Agent-Arbeit außerhalb
   des `develop`-Loops:
   - `human_gate` existiert nur im `develop`-Loop und feuert nur, wenn das Diff
     **bereits** eine ADR-Datei enthält (Pfad-Heuristik). Da der Agent die ADR
     nicht anlegt, feuert das Gate nie.
   - Der `pr-reply`-Agent kann nur einen PR-Kommentar schreiben — kein
     actionables Approval-Request.
4. Ergebnis: Die Freigabe-Bitte ist in PR-Kommentaren vergraben, es gibt keinen
   klaren Weg zu approvieren → der PR wird nie gemerged. (Beispiel: PR #72.)

**Erste Protokollversion (2026-10-18) und ihr Fehlschlag:** Die erste Version
ließ den Agent ein `needs approval`-**Issue** anlegen; ein `approval`-Loop
(`on_issue`-Trigger) pausierte am approval-Gate. Im Feld (Issue #88, 2026-10-18)
schlug das aus zwei Gründen fehl:

1. **Event-Trigger vs. Reconciling-Controller:** `on_issue` feuert nur auf
   *neue* Issues (Erst-Poll initialisiert `seen` ohne zu feuern). Issues, die
   beim ersten Poll des Loops (nach Server-Start / Spec-Reload) bereits offen
   sind, werden für immer verschluckt. Der `approval`-Loop feuerte nie (0 Runs
   im Session-Log) — der Approval-Request erschien nie in der UI.
2. **Freigabe ohne Inhalt ist sinnlos:** Der Mensch approvet eine abstrakte
   Issue-Beschreibung; erst *danach* legt der Agent die ADR an. Der Mensch sieht
   die konkrete ADR nie vor der Freigabe.

## Entscheidung

**Der Approval-Request IST der ADR-PR:** Der Agent draftet die ADR und öffnet
einen **gestapelten ADR-PR** (Base = Branch des Original-PRs, Label
`needs approval`). Der `approval`-Loop (reconcilender `on_pr`-Trigger) pausiert
am approval-Gate; der Mensch approvet via fabrik UI/CLI; der Run mergt den
ADR-PR. Die Freigabe gilt damit dem konkreten ADR-Inhalt.

**Protokoll (3 Phasen):**

1. **Request** — Der Agent (heute: `pr-reply`-Loop), der erkennt, dass eine
   Änderung eine menschliche Freigabe braucht (ADR/Spec, Architektur-Entscheidung):
   - zweigt einen ADR-Branch vom Head-Branch des Original-PRs ab,
   - draftet die ADR (`docs/adr/NNN-…md`, nächste freie Nummer, Stil der
     bestehenden ADRs: Kontext / Entscheidung / Konsequenzen),
   - öffnet einen **gestapelten ADR-PR** (Base = Original-PR-Branch, Labels
     `fabrik` + `needs approval` — beide Labels sind Pflicht für den
      Approval-Trigger, s. u.),
   - antwortet auf den auslösenden PR-Kommentar mit einem Zeiger auf den ADR-PR
     und **versucht die Änderung NICHT** auf dem Original-PR.
2. **Pause** — Der `approval`-Loop (Trigger `on_pr` + Labels `fabrik` +
    `needs approval` (AND) + `reconcile: true`) startet für den ADR-PR einen
    Run, der am `approval: true`-Gate des `pr_merge`-Steps **pausiert**. Der Run liegt
    in `needs_approval` und ist in der fabrik-UI sichtbar (freigeben/ablehnen).
3. **Grant + Merge** — Der Mensch approvet via **fabrik UI/CLI**
   (`fabrik approve fabrik <run_id>`). Der Run resumed; der `pr_merge`-Step
   squash-mergt den ADR-PR (gestapelt) mit dem Developer-Token. Der Original-PR
   kann danach re-reviewed/merged werden.

**Loop (`approval` in `projects/fabrik.fabrik.yaml`):**

```yaml
approval:
  tier: 1
  triggers:
    - type: on_pr
      repo: didaskomathetes/quark-playground
      labels: [fabrik, needs approval]   # AND-Semantik (client-seitig)
      reconcile: true
      poll: 300
  steps:
    - name: merge-adr-pr
      type: pr_merge
      repo: didaskomathetes/quark-playground
      method: squash
      approval: true        # Gate direkt am Merge → Run pausiert (needs_approval)
      approval_reason: "{pr_title}"  # PR-Titel/Link erscheinen im Approval-Kontext
```

**Amendment (PR #302):** Das Gate liegt direkt am `pr_merge`-Step statt an einem
vorgeschalteten `shell`-Step `approve`. Der Approval-Kontext (`ApprovalRequest`)
trägt PR-Link und -Titel typisiert, sodass der Mensch die konkrete freizugebende
Aktion sieht. Resume-Kompatibilität: Bereits pausierte Runs mit altem
`approve`-Step bleiben lesbar; neue Runs pausieren am Merge-Step.

**Engine-Änderung: `reconcile: true` auf `on_pr`** (opt-in, `scheduler.py`):

- Der erste Poll feuert AUCH auf *bestehende* passende PRs (nicht nur
  `seen`-Initialisierung).
- Jeder Poll feuert nur auf PRs **ohne** aktiven Run (`running`/
  `needs_approval`) — Dedup via `_has_active_pr_run` (scannt
  `runs/<projekt>/*.json` nach `loop` + `trigger` + Status).
- Ohne `reconcile` bleibt das Event-Verhalten unverändert (Erst-Poll
  initialisiert, feuert auf neu/aktualisiert).
- Zusätzlich: `on_pr` unterstützt jetzt `labels`-Filterung (zuvor ignoriert),
  mit **AND-Semantik** (client-seitig): die PR muss *alle* geforderten Labels
  haben. Die GitHub-API filtert server-seitig nur OR — ohne den client-seitigen
  AND-Filter würde `needs approval` allein auch Nicht-fabrik-PRs treffen
  (Repo `quark-playground` beherbergt mehrere Projekte; Feldbefund 2026-10-19:
  der Loop feuerte auf PRs #3/#4/#7 ohne jegliches Label).

**Warum `tier: 1`:** Der Loop vollbringt T4-nahes Werk (ADR-PR mergen). Die
Kontrolle kommt hier vom **vorderen** `approval: true`-Gate — der Mensch
approvet die konkrete ADR, *bevor* gemerged wird — und vom Cross-Model-Review
des ADR-PRs (`fabrik`-Label → `review-pr`-Loop, ADR 0003/0010), nicht von einem
synthetischen Tier-Gate am Ende. `tier: 1` setzt daher kein End-Gate; die
T4-Anforderung "menschliches Gate, kein Auto-Complete" (v1-Entwurf §1.1) ist
durch Front-Gate + Review erfüllt.

## Konsequenzen

+ Approval-Request kommt **mit der konkreten ADR** (gestapelter PR) — der
  Mensch approvet den tatsächlichen Inhalt
+ Der ADR-PR ist ein **durable, sichtbares Artefakt** (GitHub + fabrik-UI),
  nicht in Kommentaren vergraben
+ Der reconcilende Trigger ist **robust gegen Restarts/Spec-Reloads** —
  bestehende ADR-PRs werden aufgegriffen, keine Doppel-Runs
+ Freigabe über den **bestehenden** fabrik-Mechanismus (UI/CLI) — ein
  konsistenter Approval-Weg, kein zweites Protokoll
+ Kein Deadlock: Agent draftet ADR + öffnet PR, Mensch approvet, fabrik mergt
+ Der ADR-PR durchläuft die normale Review-Pipeline (`fabrik`-Label →
  `review-pr`, `sonar-scan`)
- Ein zusätzlicher Loop + ein Run pro Approval-Request
- Die ADR wird vom Agent (Builder-Modell) **gedraftet** — die menschliche
  Freigabe ist das Gate; Draft-Qualität hängt vom Agent ab
- Bei Reject (`fabrik reject`) endet der Run; der ADR-PR bleibt offen und wird
  manuell geschlossen
- Gestapelter PR: Der ADR-PR muss **vor** dem Original-PR gemerged werden
  (Base = PR-Branch)
- Merge-Konflikte: Wenn der Original-PR voranschreitet (Code-Fix via
  `pr-reply`), kann der gestapelte ADR-PR im Konflikt sein. Der `pr-reply`-Agent
  rebaset gestapelte ADR-PRs (Base = PR-Branch) nach jedem Push auf den
  Original-PR-Branch (Prompt-Schritt 5b) — Konflikt-Prävention, nicht
  -Auflösung im Merge-Step.

## Alternativen (abgelehnt)

- **`needs approval`-Issue + `on_issue`-Loop (erste Version, 2026-10-18):**
  Event-Trigger verschluckt bestehende Issues (Erst-Poll initialisiert);
  Freigabe einer abstrakten Beschreibung ohne ADR-Inhalt ist sinnlos. Im Feld
  fehlgeschlagen (Issue #88).
- **Approval per GitHub-Kommentar** (`approved` im Issue/PR): zweites,
  paralleles Approval-Protokoll neben `fabrik approve` — inkonsistent, zwei
  Wege zur selben Freigabe.
- **Nur PR-Kommentar mit Marker:** genau das Problem, das wir lösen
  (in Kommentaren vergraben, nicht actionabel).
- **`human_gate` auf `pr-reply` ausweiten:** `human_gate` ist ein
  Diff-Kritizitäts-Check für den `develop`-Loop; hier braucht der Agent aber
  eine **proaktive** Freigabe-Bitte, bevor er die ADR anlegt — andere Semantik.
- **Cron-basierte Reconciliation** (alle N Minuten `needs approval`-PRs
  listen): mehr bewegliche Teile, pro-PR-Run-Dedup wird trotzdem gebraucht;
  `reconcile: true` erreicht dasselbe mit einer kleineren, testbaren
  Änderung am bestehenden Trigger.
