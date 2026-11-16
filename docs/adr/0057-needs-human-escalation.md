# ADR 0057: `needs human` — Eskalation an einen Menschen bei Blockade, Stagnation und Scope-Drift

**Status:** Vorgeschlagen (2026-11-16)
**Ergänzt:** ADR 0011 (Approval-Request-Protokoll), ADR 0013 (AC-Verifikation im Review),
ADR 0024 (Verdikt-Marker, Reply-Sperre),
ADR 0035 (Fehlerklassen), ADR 0036 (Run-Retry, erschöpfte Aufträge), ADR 0039 (konvergente
Review-Runden).

## Kontext

PR example-org/example-repo#1282 (Issue #1078) lief vier Review-Runden
(2026-11-13 dreimal, 2026-11-16 einmal), ohne dass sich etwas bewegte:

- Der Reviewer meldete jede Runde dieselben zwei Findings: Die Live-Allow-List ist nicht
  angepasst (P1), und es fehlt der Nachweis aus dem Fixture-Shop (P2). Verdikt jeweils
  `changes_requested`.
- Der Builder antwortete jede Runde: „korrekt, aber nicht von hier lösbar, braucht einen
  Menschen" und änderte nichts. Die Live-Liste liegt in einem anderen Repo, der Fixture-Shop
  ist aus der Sandbox nicht erreichbar.
- Jede Antwort und jeder neue Head löste den nächsten Review aus. Das „braucht einen
  Menschen" stand nur als Freitext in einem PR-Kommentar. Niemand wurde benachrichtigt, und
  in der fabrik-UI war nichts sichtbar.

Das ist derselbe Fehler, den ADR 0011 für ADRs behoben hat: Die Bitte um eine menschliche
Entscheidung ist in PR-Kommentaren vergraben, und es gibt keinen Weg, darauf zu handeln.
ADR 0011 deckt aber nur den Fall „Änderung braucht eine ADR" ab. Es fehlt ein allgemeiner
Kanal für die übrigen Fälle, in denen ein Loop nicht von selbst weiterkommt:

1. **Blockade:** Der Builder braucht etwas außerhalb seiner Reichweite: Zugang (Live-Shop,
   Fixture-Shop), eine Änderung in einem fremden Repo oder bei einem anderen Team, eine
   Policy- oder Produktentscheidung.
2. **Stagnation:** Review und Reply drehen sich, das Verdikt ändert sich nicht (#1282).
3. **Erschöpfung:** `pr_review` erreicht `max_rounds` (ADR 0039), eine Retry-Kette ist
   `exhausted` (ADR 0036). Heute endet beides als `fail` im Run-Log, ohne dass jemand eine
   Entscheidung treffen muss.
4. **Scope-Drift:** Die Lösung weicht deutlich von den Acceptance Criteria des Issues ab oder
   ist viel größer als das Issue vermuten ließ (anderes Repo, anderes Team, Architektur).

## Entscheidung

### 1. Ein Zustand, ein Label, ein Gate

Alle vier Fälle münden in **denselben Eskalationszustand**. Er fühlt sich an wie eine
Freigabe: Der Mensch sieht in der fabrik-UI/CLI einen wartenden Run mit Begründung und
handelt dort.

- **Dauerhafter Zustand = Label** `needs human` am PR (bzw. am Issue, wenn kein PR existiert).
  Der Labelname ist pro Projekt konfigurierbar (`human_label`, Default `needs human`).
- **Sichtbarkeit = Run im Gate:** Ein `escalation`-Loop (Trigger `on_pr`/`on_issue`, Labels
  `[fabrik, needs human]`, `reconcile: true`, analog zum `approval`-Loop aus ADR 0011)
  startet pro markiertem Item einen Run, der in `needs_approval` pausiert. Der
  `ApprovalRequest` bekommt den neuen `reason` `escalation` und trägt:
  `kind` (s. u.), `why` (Zusammenfassung des Blockers), `what` (die Optionen für den
  Menschen), `pr`/`issue` und Links auf die auslösenden Kommentare.
- **Benachrichtigung:** wie bei Freigaben (UI-Badge, `notify`-Step, falls konfiguriert).

### 2. Eskalationsarten (`kind`)

Feste, kleine Menge. Neue Arten nur per ADR-Änderung.

| `kind` | Auslöser | erkannt durch |
|---|---|---|
| `blocked` | Builder braucht Zugang, eine Änderung in fremdem Repo/Team oder eine Entscheidung; oder ein AC ist aus der Sandbox nicht prüfbar | Marker des Builders (§3); AC-Verifikation des Reviewers (§3) |
| `stalled` | `stall_rounds` (Default 2) aufeinanderfolgende `changes_requested`-Verdikte, ohne dass der Builder dazwischen Code geändert hat, oder ohne neue Findings (ADR 0039: Reviewer meldet nur Vorrunden-Findings als offen) | deterministisch aus Verdikt- und Reply-Markern (§4) |
| `exhausted` | `pr_review` nach `max_rounds` weiter `changes_requested`; Retry-Kette `exhausted` (ADR 0036) | Engine, an der Stelle, an der heute `fail` gesetzt wird |
| `scope` | Lösung erfordert deutlich mehr als die ACs, oder ein blockierendes Finding liegt außerhalb der ACs | Marker des Builders (§3) oder Finding-Feld `out_of_scope` des Reviewers (§3) |

### 3. Marker statt Freitext

Der Builder eskaliert über einen **strukturierten Marker** in seiner Antwort, nicht über die
Formulierung „needs human":

```
<!-- fabrik-escalate kind=blocked re=<keys> -->
```

- `post-replies` (host-seitig, `pr-status.py`) validiert den Marker wie die übrigen: `kind`
  nur aus der Tabelle, `re` nur aus den Keys dieses Runs. Erst der Host setzt das Label.
  Der Agent erhält dafür keine zusätzlichen Rechte.
- Der Reviewer darf pro Finding `out_of_scope: true` setzen (Erweiterung des Finding-Schemas
  aus ADR 0039). Ist ein blockierendes Finding (`high`/P1) `out_of_scope`, eskaliert der
  Review-Step mit `kind=scope`, statt `changes_requested` zu posten und den Builder
  loszuschicken.
- **AC-Verifikation (ADR 0013 Stufe 2):** Der Reviewer meldet pro Kriterium
  `met`/`unmet`/`unverifiable`. Ist ein Kriterium in zwei aufeinanderfolgenden Runden
  `unverifiable` (im Beispiel: TICKET-REDACTED gegen einen Fixture-Shop), eskaliert der Review-Step
  mit `kind=blocked`. Er schickt den Builder nicht erneut los, denn der kann es per Definition
  nicht nachweisen. `unverifiable` blockiert das Verdikt weiterhin nicht (ADR 0013); es stoppt
  nur die Schleife.
- **Vorher alles Machbare tun:** Bevor der Builder eskaliert, erledigt er, was in seiner
  Reichweite liegt und nicht die Entscheidung des Menschen vorwegnimmt (z. B. Tests ergänzen,
  den PR-Body um den tatsächlich umgesetzten Scope ergänzen). Die Eskalation nennt, was
  erledigt ist und was offen bleibt.

### 4. Stopp: Loops respektieren das Label

- `review-pr`, `pr-reply` und `develop` überspringen Items mit `needs human`. Das gilt am
  Trigger (Label-Ausschluss) und zusätzlich im Gate von `pr-status.py` (`reply-context`,
  `merge-check` → `skip`), damit ein manueller Run nicht durchrutscht.
- Die Stagnationserkennung (`stalled`) läuft deterministisch in `pr-status.py` aus den
  vorhandenen Markern (`fabrik-review verdict=… sha=…`, `fabrik-reply re=…`) und der
  Commit-Historie des PR-Branches. Sie nutzt kein LLM.

### 5. Aktionen des Menschen

Im Gate der fabrik-UI/CLI, mit derselben Bedienung wie Freigaben:

| Aktion | Wirkung |
|---|---|
| **Fortsetzen** (optional mit Notiz) | Label entfernt. Die Notiz wird als PR-Kommentar vom Account des Menschen gepostet und ist damit regulärer Input für `pr-reply` (z. B. „MAS-PR ist gemergt, bitte erneut verifizieren"). Die `on_pr`-Reconcile-Trigger feuern wieder. |
| **Neu zuschneiden** | Wie Fortsetzen. Der Mensch hat vorher ACs oder PR-Body angepasst (z. B. `Closes` → `Refs`). Die Notiz sagt dem Builder, was sich geändert hat. |
| **Aufgeben** (= Ablehnen) | PR geschlossen mit Begründungskommentar. Issue bleibt offen, `needs human` bleibt am Issue, damit `develop` es nicht neu aufgreift. |

Löst der Mensch das Problem direkt auf GitHub (Label manuell entfernt), endet der
Eskalations-Run beim nächsten Reconcile von selbst als erledigt.

### 6. Verhältnis zu bestehenden Mechanismen

- **ADR 0011** bleibt für den Fall „ADR nötig" maßgeblich: Dort gibt es konkreten Inhalt zum
  Freigeben (den ADR-PR). `needs human` ist für Fälle ohne solchen Inhalt.
- **ADR 0035/0036:** `environment`-Fehler alarmieren weiter über den Alert-Pfad (ADR 0025).
  Nur Ketten, deren letzter Fehler `outcome` war, werden zusätzlich zu `exhausted`-Eskalationen.
  Infrastrukturprobleme werden also keine PR-Labels.

## Konsequenzen

+ Review-/Reply-Schleifen wie bei #1282 enden spätestens nach `stall_rounds`. Keine weiteren
  Token für Runden, die nichts ändern.
+ Ein Kanal, eine Bedienung für alle „braucht einen Menschen"-Fälle. Die Bitte steht in der
  UI, nicht in Kommentar Nr. 7.
+ Erschöpfte Fix-Runden und Retry-Ketten werden zu Entscheidungen statt zu stillen `fail`s.
+ Der Agent bekommt keine neuen Rechte. Label und Run setzt der Host nach Validierung.
- Ein neuer Loop (`escalation`) und ein neuer `ApprovalRequest.reason`. Die UI muss
  `escalation` von normalen Freigaben unterscheiden (Text, Aktionen).
- Ein falscher `blocked`-Marker parkt einen PR, den der Builder hätte lösen können. Gegenmittel:
  Der Marker verlangt die Liste „erledigt / offen", und der Mensch kann mit Notiz fortsetzen.
- `stall_rounds` zu niedrig stoppt Schleifen, die gerade konvergieren. Der Default 2 ist
  vorsichtig gewählt und pro Loop überschreibbar.

## Offene Punkte (nicht Teil dieser Entscheidung)

- Automatisches Anlegen eines Folge-Issues im fremden Repo bei `kind=blocked`, wenn der
  Blocker ein konkreter Code-Change ist (im Beispiel: `merchant-assistant-service`). Braucht
  Schreibrechte dort und eine Regel, wer das darf.
- Eskalation aus dem `refine`-Loop (ADR 0006/0017), wenn ein Issue schon beim Verfeinern als
  nicht in diesem Repo lösbar erkennbar ist.
