# ADR 0017: Refine-Loop — Human-Gate nur bei `needs approval` / `close`

**Status:** Akzeptiert (2026-10-19); Änderung 2026-10-21 akzeptiert (2026-10-21)
**Entscheider:** Dominic
**Änderung 2026-10-21:** Close als Vorschlag mit Label `close proposed`,
Kriterien für `needs approval` (siehe §„Änderung 2026-10-21").
**Ersetzt:** ADR 0006 §„Human-Gate" sowie ADR 0001 §1–2 (Triage-Gate als
Human-Gate). Der Rest von ADR 0006 (Steps, Label-Semantik, Close-Pfad) gilt
weiter.

## Kontext

ADR 0006 setzte ein statisches `approval: true` auf `apply-label`: **jeder**
refine-Run pausierte, auch wenn das Issue bereits fertig spezifiziert war und
als `ready for development` klassifiziert wurde (Feldbefund 2026-10-19, Issue
#16: Issue mit vollständigen Annahme-Kriterien musste trotzdem freigegeben
werden).

Das widerspricht der späteren Linie:
- ADR 0010: „PR-Pipeline ohne Menschen im Loop — außer bei kritischen
  Entscheidungen". Der `develop`-Loop hat dafür `human_gate` (ADR-Pfade,
  Diff-Größe, Keywords, LLM) plus Cross-Model-PR-Review.
- framework-v1-draft, Leitprinzip 3: „Mensch sitzt nur an
  irreversiblen/Accountability-Gates."

ADR 0001 §2 begründete das Triage-Gate damit, dass es *das* Human-Gate für
`develop` sei. Seit ADR 0010 sichert `develop` sich selbst ab — das
Refine-Gate für unkritische Issues ist damit redundant.

Zusätzlich war `needs approval` doppelt manuell: Die Freigabe setzte nur das
Label `needs approval`, danach musste der Mensch auf GitHub von Hand
umlabeln.

## Entscheidung

Der Mensch entscheidet im refine-Loop **nur noch**, wenn der Classify-Step
Architektur-Impact / Widerspruch zu ADRs (`needs approval`) oder einen Close
erkennt:

| `classify` | vor der Pause | Approve | Reject |
|---|---|---|---|
| `ready for development` | Label sofort, **keine Pause** → `develop` startet | — | — |
| `needs approval` | Label `needs approval` sofort (sichtbar auf GitHub) | `needs approval` → `ready for development` (develop startet) | Label bleibt, Run endet; Mensch entscheidet manuell |
| `close proposed` | Label `close proposed` + Abschnitt „Close-Vorschlag" im Body sofort | Begründungs-Kommentar + Close | Label bleibt, Run endet; Mensch entscheidet manuell |

**Engine: bedingtes Gate `approval_when`** (Step-Feld, `engine._approval_condition`):

```yaml
approval_when:
  label: "needs approval"      # Wert oder Liste von Werten
```

- Pausiert VOR dem Step nur, wenn jede genannte Run-Variable einen der Werte
  hat (AND über Variablen, OR über Werte, Vergleich als String). Fehlende
  Variable → kein Gate.
- Gleiche Resume-Semantik wie `approval: true`: der gepauste Step läuft nach
  Approval ohne erneutes Gate. Der Approval-Kontext trägt die erfüllte
  Bedingung (`approval.condition`, Log-Zeile `bedingung:`).
- Nicht mit `approval: true` kombinierbar (Spec-Validierung).
- Ersetzt das Tier-Gate (T2+) **nicht** — nur statisches `approval: true` tut
  das, weil `approval_when` unerfüllt bleiben kann.

**Refine-Loop:** `classify → … → propose-close → apply-label →
promote (approval_when: needs approval) → close-issue (approval_when: close proposed) →
report`. Der Classify-Prompt sagt explizit, dass `ready for development` ohne
Freigabe in die Entwicklung geht. Die Kriterien für `needs approval` stehen im
classify-Prompt (`projects/fabrik.fabrik.yaml`) und werden von
`tests/test_fabrik_spec.py` festgehalten; im Zweifel `ready for development`.

## Begründung

- **Ein Gate pro kritischer Entscheidung:** unkritische Issues werden in
  `develop` durch `human_gate` + Review abgesichert; doppelte Gates kosten
  Aufmerksamkeit und trainieren reflexhaftes Durchwinken.
- **Freigabe = Entscheidung:** Approve bei `needs approval` heißt „darf
  entwickelt werden" und setzt das passende Label — kein Nacharbeiten auf
  GitHub.
- **Close bleibt gegated** (Begründung aus ADR 0006 unverändert): falsche
  Duplikat-Erkennung ist für den Reporter kaum umkehrbar.
- **`approval_when` statt Step-Ergebnis-Gate:** Die Bedingung steht
  deklarativ in der Spec (prüfbar, testbar), statt in Shell-Logik versteckt;
  `human_gate` bleibt für dynamische, diff-basierte Kritikalität (ADR 0010).

## Konsequenzen

+ Unkritische Issues fließen ohne menschliche Interaktion in `develop`
+ `needs approval`-Issues sind auf GitHub sofort als solche markiert
+ Generisches Engine-Feature, auch für andere Loops nutzbar
- Die Classify-Einschätzung (LLM) entscheidet jetzt, ob ein Mensch das Issue
  vor der Entwicklung sieht. Fehlklassifikation `ready for development` statt
  `needs approval` fängt erst `human_gate` am PR-Diff ab.
- Bereits pausierte refine-Runs (alte Spec) laufen nach Approve mit der neuen
  Spec weiter: `needs approval`-Runs pausieren dann ein zweites Mal am
  `promote`-Step (einmaliger Übergangseffekt).

## Abgelehnte Alternativen

- **Gate komplett entfernen:** Close ohne Freigabe ist zu riskant (ADR 0006).
- **Bedingung in Shell (`exit` + Step-Result):** Shell-Steps können nicht
  pausieren; ein eigener Step-Typ nur dafür wäre mehr Fläche als ein Feld.
- **`needs approval` weiterhin nur labeln (ohne Pause):** dann gäbe es keinen
  Freigabeweg in der fabrik-UI; der Mensch müsste auf GitHub suchen.

## Änderung 2026-10-21 (Akzeptiert)

**Kontext:** Der classify-Prompt nannte Themen statt Gründe und endete mit
„im Zweifel: `needs approval`" — fast jedes Issue mit Engine-Bezug wurde gegated
(z. B. #241, #237). Ein gegateter Close war nur in der fabrik-UI sichtbar, nicht
auf GitHub.

**Entscheidung:**
- `needs approval` nur bei benanntem Grund (Architektur/ADR, sehr groß,
  kritisch); im Zweifel `ready for development`. Die Gründe stehen im Prompt.
- Close wird zum Vorschlag: Label `close proposed` und Begründung im Body
  sofort, Close erst nach Freigabe — für jeden `close_kind`. Werte und Folgen
  definiert ADR 0031: `duplicate` (mit `duplicate_of`; Blocker der Abhängigen
  wandern auf das Original), `completed` (gibt abhängige Issues frei),
  `not_planned` (Abhängige bleiben blockiert); leer = GitHub-Default `completed`.

**Abgelehnte Alternativen:**
- *Duplikat/„bereits gelöst" ohne Gate schließen:* Ein Reopen triggert refine
  nicht erneut (Reconcile merkt sich die Issue-Nummer), und ein `completed`-Close
  gibt abhängige Issues sofort für develop frei (ADR 0031). Fehler wären still
  und teuer, die Freigabe eines Close dagegen selten und schnell.
- *Reject → `needs approval`:* bräuchte einen Reject-Hook in der Engine; der
  manuelle Weg genügt und entspricht dem Reject von `needs approval`.
