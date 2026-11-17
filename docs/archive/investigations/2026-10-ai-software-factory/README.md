> **Archived:** completed investigation (relay run of 2026-10-21/22, audited, verdict pass). Kept as a historical research report.

# Investigation: AI software factory — patterns, concepts, prompts, error scenarios (2026-10)

Desk research on public sources about agentic development pipelines ("AI software factories"),
measured against fabrik. Produced by the first real **relay** run (ADR 0037): two agent sessions
(builder model) plus the two-phase audit (reviewer model), 2026-10-21/22.

| File | Content |
|---|---|
| [report.md](report.md) | Summary, findings by category, **12 gaps in fabrik** with supporting findings, sources, further leads |
| [findings.md](findings.md) | 44 findings (paragraph + category + source URL) |
| [leads.md](leads.md) | Further sources, noted only, not researched |
| [audit.md](audit.md) | Independent audit: verdict **pass**, reconciliation table, findings, spot-check list |

Sources: Cloudflare blog, Pragmatic Engineer, aihero.dev, all-ai.de. Reddit was blocked (HTTP 403)
on its search endpoint.

## Caveats (from the audit)
- The "For fabrik …" sentence at the end of most findings is the agent's interpretation, not the
  source's claim. The ADR references in those sentences were not verified.
- F16's title says "measured" productivity claims; the source and the finding's body say they
  are estimates.
- Not spot-checked: F3/F5 (percentages), F37/F38 (paraphrased from German), F40–F44.
- Whether each gap is really supported by the findings it cites is a judgement call for a human.
- Copied unchanged from the relay branch except for removed repository paths and IDs; the
  per-session scripts and logs are not included.
