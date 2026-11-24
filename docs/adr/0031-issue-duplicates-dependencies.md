# ADR 0031: Duplikat- und Abhängigkeits-Erkennung im Refine, Blocker-Gate im Develop

**Status:** Akzeptiert (2026-10-21, Merge #250; vorgeschlagen 2026-10-20)
**Ergänzt:** ADR 0006/0017 (Refine-Loop), ADR 0014/0021 (`on_issue`-Reconcile),
ADR 0018 (`issue`-Step: Actions `list` und `block`; neue Felder für `create` und `close`).

## Kontext

- Der Classify-Prompt verlangte schon „Duplikat → close", sah aber nur das eigene
  Issue. Duplikate konnte er also nur raten.
- `develop` startete jedes `ready for development`-Issue sofort. Issues, die auf
  einem anderen, noch offenen Issue aufbauen, liefen gegen eine fehlende Basis.
- KI-generierte Issues setzen oft Grundlagen voraus, die **kein Issue** abdeckt.
  Feldbefund 2026-10-20: #19 (Postgres) und #21 (Reaper) unterscheiden Deployment-Profile
  `homelab|cloud`. Den Mechanismus gibt es nur als Teil von #28 (Cloud-Rollout, p3), ein
  Homelab-Profil gar nicht. Zudem haben die bereits refinten Issues keine
  Blocker-Relationen: #160 sagt „Abhängig von #159“ nur im Text.

## Entscheidung

1. **Single Source of Truth: native GitHub-Issue-Dependencies** („blocked by“).
   Wir nutzen keinen Marker im Body und keinen eigenen State. Menschen sehen und
   ändern die Relation in der GitHub-UI.
2. **Refine-Loop:** `list-open → list-closed → classify → dependencies → refine-body →
   create-prerequisite → link-blockers → apply-label → promote → close-issue → report`.
   - `list-open`/`list-closed` (`issue`-Action `list`): offene fabrikant-Issues (≤ 100) und
     die 30 zuletzt geschlossenen als Textliste `#N [Labels] Titel — Auszug`. Die
     Listen gelten als untrusted.
   - `classify` prüft Duplikate. Ein Duplikat führt zu `close` mit `close_kind` und
     `duplicate_of`, hinter dem bestehenden Close-Gate (ADR 0017).
   - `dependencies` (eigener LLM-Step, fokussierter Prompt) liefert `blocked_by`
     (≤ 5 offene Issues; Tracking-Issues bündeln oft so viele Teil-Issues) und höchstens **eine** fehlende Voraussetzung
     (`prerequisite_title/body`): die kleinste Grundlage, die weder umgesetzt ist
     noch von einem offenen Issue abgedeckt wird.
   - `create-prerequisite` legt die Voraussetzung als Issue mit Label `fabrikant` an.
     Sie durchläuft damit selbst refine (Duplikat-Check, `needs approval` bei
     Architektur) und erbt die Priorität des abhängigen Issues (`inherit_labels`,
     Whitelist `p0–p3`, nie Status-Labels).
   - `link-blockers` (Action `block`) verknüpft `blocked_by` und die neue
     Voraussetzung idempotent. Halluzinierte Nummern, geschlossene Issues und PRs
     werden übersprungen, höchstens 6 (5 Blocker + die Voraussetzung). Andere API-Fehler lassen den Step fehlschlagen.
   - Beide Steps laufen **vor** `apply-label` und nur bei `ready for development` /
     `needs approval`. `develop` sieht das Label also nie ohne die zugehörigen
     Blocker. Schlägt ein Step fehl, bekommt das Issue kein Label (fail closed).
3. **Erledigt = `completed`:** Ein Blocker gilt nur als erledigt, wenn er als
   `completed` geschlossen ist (oder ohne Grund, bei Alt-Issues). Als
   `duplicate`/`not_planned` geschlossene Blocker blockieren weiter, denn die
   Vorarbeit fehlt dann noch. Deshalb schließt refine mit explizitem `state_reason`.
   Ein Duplikat-Close mit `duplicate_of` hängt die Relationen der Abhängigen auf das
   Original um, bevor er schließt. Schlägt das fehl, bleibt das Issue offen.
4. **Gate im Scheduler** (`on_issue`-Option `skip_blocked: true`, nur mit
   `reconcile`): Issues mit unerledigten Blockern starten nicht und hinterlassen
   keinen Run-Record. Sie feuern beim ersten Poll, nachdem der letzte Blocker
   erledigt ist; bei `develop` ist das der PR-Merge über `Closes #N` (ADR 0013).
   `issue_dependencies_summary.total_blocked_by` spart den Dependency-Call bei Issues
   ohne Blocker. API-Fehler brechen den Poll ab (fail closed). Blockierte Issues
   belegen kein `max_per_poll`-Kontingent. Der Event-Modus ist ausgeschlossen
   (Spec-Validierung).
5. **Einmaliger Backfill** (`deps-backfill`-Loop): dieselben Step-Bausteine (YAML-Anker
   unter `x-shared-steps`), ohne Relabel/Close, einmal pro offenem fabrikant-Issue.
   Er läuft mit 1 Run pro 120 s, damit ein Run die Voraussetzung des vorigen sieht.
   Ablauf: `develop` pausieren → Backlog abarbeiten → `deps-backfill` deaktivieren →
   `develop` fortsetzen.

## Konsequenzen

+ Die Develop-Seite ist deterministisch: nichts startet mit unerledigten Blockern,
  egal ob Refine, Backfill oder ein Mensch den Blocker gesetzt hat.
+ Fehlende Grundlagen werden zu eigenen, priorisierten Issues, statt dass `develop`
  sie implizit mitbaut oder an ihnen scheitert.
- Die **Erkennung** ist eine LLM-Einschätzung auf Titel- und Auszug-Basis, ohne Blick
  in den Code:
  - Ein falscher Blocker stoppt das Issue, bis ein Mensch die Relation entfernt.
    Das ist sichtbar als Log-Zeile „wartet auf Blocker", aber noch nicht in der UI.
  - Ein fehlender Blocker verhält sich wie bisher.
  - Eine Voraussetzung kann schon im Code existieren, ohne dass ein Issue sie nennt.
    Der develop-Run dazu wird dann klein oder leer.
- **Voraussetzungs-Ketten:** Eine Voraussetzung kann bei ihrem Refine eine weitere
  erzeugen (pro Run höchstens eine). Architektur-Grundlagen landen typischerweise bei
  `needs approval`, also bei einem Menschen.
- **Doppelte Voraussetzungen:** Parallele Runs können dieselbe Voraussetzung anlegen.
  Der Duplikat-Check der zweiten schließt sie mit `duplicate_of` und hängt die Blocker
  um. Dieser Close ist gegated.
- Ein Blocker, der nie als `completed` schließt (z. B. abgelehntes `needs approval`),
  blockiert dauerhaft. Das ist gewollt, ein Mensch entscheidet. Zyklen blockieren
  beide Issues.
- Bei > 130 Issues reicht die Kontext-Liste nicht mehr. Dann wäre eine Vorauswahl nötig
  (GitHub-Suche, Embeddings).
- Der `refine`-Loop macht einen LLM-Call mehr pro Issue (`dependencies`).

## Abgelehnte Alternativen

- **`Depends on #N` im Body + Parser:** Das wäre ein zweites Format neben der
  GitHub-Relation, und es wäre fragil.
- **Fehlende Voraussetzung nur eskalieren (`needs approval` statt Issue):** Das ist
  sicherer gegen Issue-Wildwuchs, macht aber jeden Fall manuell. Das widerspricht
  ADR 0017. Der Mensch sieht Architektur-Grundlagen ohnehin über das Refine der
  Voraussetzung.
- **Jeder Close gibt Abhängige frei:** Ein als Duplikat oder `not_planned`
  geschlossener Blocker würde Issues ohne ihre Grundlage starten lassen.
- **Blocker-Check im Develop-Agent oder in `_safe_run`:** Im Agent wäre der Run schon
  gestartet, und eine LLM-Entscheidung würde das Gate bilden. In `_safe_run` würden
  auch manuelle Runs geblockt.
