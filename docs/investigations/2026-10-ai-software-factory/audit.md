# Relay run — audit (phase 2)

Date: 2026-10-22. Access: this checkout (read-only) and public web pages via read-only `curl` (scratch files in `a scratch directory`). Nothing was written to any system. Only `the audit scratch directory` was edited.

## Summary

All five fixed invariants hold. The verify block reproduces the worker's result exactly (`44 findings, 44 sourced, 44 categorised, sites 4/5`). `report.md` has the four category sections plus "Gaps in fabrik", and each of the 12 gaps cites 3–5 findings. The branch changes only the 20 files under ``. Five randomly sampled findings (F8, F15, F16, F25, F39) were checked against their live pages: each page exists, is public, and supports the paragraph, including every number checked. The Reddit block reproduces byte-for-byte (403, 189 908 bytes). The findings are minor: one stale bullet in the handoff, a title that contradicts its own body, `findings.md`/`leads.md` written by hand with no script, and a "Reddit inaccessible" claim that is broader than what was actually tried. None of them is a blocker.

## Reconciliation table

Phase 1 left invariants 1–5 inconclusive because the audit checkout had the deliverables removed. Phase 2 measured all of them.

| # | Invariant | Expected | Worker's claim | Measured (phase 2) | Result |
|---|---|---|---|---|---|
| 1 | Every finding has `Category:` from the list and `Source:` with a full URL | count(F) = count(Source) = count(Category) | 44/44/44 | 44 findings, 44 `Source: https://…`, 44 valid `Category:` | ✅ |
| 2 | Every source URL fetched in a recorded command; 5 spot-checks exist and support the finding | 5/5 | "All source pages fetched by recorded scripts" | All 12 distinct source URLs appear in a recorded script or in a logged `s1-02` invocation (see N3, N4). Spot-check 5/5: pages return 200 and support the paragraph | ✅ |
| 3 | ≥ 25 findings from ≥ 4 of 5 sources; blocked sources documented | n ≥ 25, sites ≥ 4 | 44, 4/5, Reddit blocked | 44, 4/5 (cloudflare 9, pragmaticengineer 18, aihero 14, all-ai.de 3, reddit 0). Reddit search.json still 403 with identical size. Documented in `report.md` § "Sources that could not be accessed" | ✅ (see N5) |
| 4 | `report.md` exists with the sections; every gap cites ≥ 1 finding | all present, every gap linked | 12 gaps, each cites findings | Patterns 17, Concepts 14, Prompts 6, Error scenarios 7 (sums to 44). Gaps cite 5,3,3,4,3,3,5,4,3,4,4,4 findings. 44 distinct finding anchors, all of which exist | ✅ |
| 5 | No file outside `./` changed | diff only in relay dir | "Only  changed" | `git diff --stat main...HEAD`: 20 files, 580 insertions, all under ``. Two commits on the task branch | ✅ |

## Findings

| ID | Severity | Finding | Where |
|---|---|---|---|
| N1 | should fix | **The handoff `## State` contradicts itself.** One bullet still says "findings.md: F1-F39 … Verify counts should read 39 findings". The s2 bullet and the frontmatter say 44. A reader of the State section alone gets the wrong count. | `HANDOFF.md`, `## State`, 2nd bullet |
| N2 | should fix | **F16's title says "measured productivity claims", but its body (correctly) says "These are his own estimates, not measurements."** The source supports the body: "Dex says…", "Dex believes…". The title overstates the evidence, and the report's Concepts list repeats it. | `findings.md` F16 heading; `report.md` Concepts section |
| N3 | note | **`findings.md` and `leads.md` have no script that writes them.** They were written by the agent directly, including the s2 append of F40–F44. That is reasonable for authored research text, but the audit trail for these two files is the git history (two commits), not a script. `report.md` is regenerable (`s2-03`). | `sessions/s2.md` line 2; `scripts/` |
| N4 | note | **`s1-02-fetch-article.sh` takes the URL as an argument, so the Cloudflare article URLs exist only in `sessions/s1.md` and not in any script file.** The log names all five slugs, and the two that are cited (`obsessive-inference-optimization`, `task-based-oauth-consent`) match. "Fetched by a recorded command" holds through the log, not the scripts. | `sessions/s1.md` line 3; `scripts/s1-02-fetch-article.sh` |
| N5 | note | **"Reddit inaccessible" is broader than what was tried.** Only the r/ExperiencedDevs *search* endpoints were probed (`search.json`, `old.reddit.com/…/search`), and both return 403. In phase 1, a subreddit listing page (`https://www.reddit.com/r/ClaudeAI/`) returned 200. Reddit is partly reachable without login. This does not affect the 4/5 invariant, and the worker respected the one-retry limit and did not attempt a bypass. | `report.md` § "Sources that could not be accessed"; `the audit scratch directoryblind/03-sources-reachable.out` |
| N6 | note | **Pragmatic Engineer content was fetched from the Substack API (`/api/v1/posts/<slug>`), but the findings cite the `/p/<slug>` web URLs.** I checked that the `/p/` pages are public (200) and that their HTML text carries the sampled claims (F15, F16, F25), and that the API reports `audience: everyone` for both sampled posts. The citations are valid. The fetched URL and the cited URL just differ. | `scripts/s1-05`, `s1-06`; `findings.md` F10–F27 |
| N7 | note | **`s1-05` downloaded `why-ramp-built-inspect`, which is a paid post** (partial/preview body). The log and the report say it was not used, and no finding cites it. That matches: there are 0 `Source:` lines for it. The fetch itself was unauthenticated and served the public preview only. | `scripts/s1-05-pragmatic-posts.sh`; `sessions/s1.md` line 6 |
| N8 | note | Failed and retried commands are documented, not swallowed. `s1-03` returned HTTP 400 and was retried as `s1-04` without the bad parameter. aihero `/articles` 404 is logged. `s1-01` has a dead loop (`for u in "$@"; do :; done`) with no effect. No script ran twice except the documented, permitted Reddit retry (`s2-01`). | `scripts/s1-01`, `s1-03`, `s1-04` |

**Safety:** this is a research task with no external writes. Every network call is a read-only `curl` GET. Scratch output went to `a scratch directory`, and repo changes stay inside the relay directory. No backup or snapshot was required. A grep for credentials (`AKIA`, `Bearer`, `password`, `secret`, `token`) in the relay directory matched only prose in the findings. No secrets in the files.

## Unspecified transformations (for a human decision)

The brief only specifies "findings paraphrased". The following choices were the worker's own:

1. **HTML → text extraction drops content.** `s1-02`, `s1-08` and `s1-11` keep only the first `<article>` (or `<main>`) element and strip `script`/`style`/`svg`. `s1-06` keeps only `body_html`. Any content outside these elements (sidebars, footnotes, comments) was never seen by the worker. This is unlikely to matter, but it is a silent filter.
2. **fabrik-specific editorialising inside findings.** Most findings end with a "For fabrik …" sentence that maps the source to fabrik ADRs. Those sentences are the worker's interpretation, not the source's claims. The ADR references were taken from the fabrik README/ADR index, and I did not verify them.
3. **The gap list is hard-coded in `s2-03-build-report.sh`.** Only the category lists and the sources are generated from `findings.md`. The 12 gaps and the executive summary are fixed text, so editing `findings.md` and re-running the script will not update them.
4. **Some paraphrases aggregate figures.** For example, F8 gives "40–71% fewer findings" as a range across three reviewers (code quality 40%, security 56%, performance 71%). This is accurate, but it compresses the source.
5. **`leads.md` is listed through `sort -u`**, so lead order is alphabetical, not by relevance.

## Sample: what a human should spot-check

| Key | Check | Where |
|---|---|---|
| F8 | ✅ checked: 40/56/71% fewer, 61% cost, 58% more findings, "authors act on" | https://blog.cloudflare.com/obsessive-inference-optimization/ |
| F15 | ✅ checked: nightly, four agents → four PRs, human reads before merge | https://newsletter.pragmaticengineer.com/p/context-engineering-with-dex-horthy (item 10) |
| F16 | ✅ body checked (lights-off failed, 30–50%, 2–3x). Title overstated (N2) | same page, item 12 |
| F25 | ✅ checked: review bottleneck, AI review run, screenshots/videos, risk scores | https://newsletter.pragmaticengineer.com/p/ubers-ai-infra-from-the-inside (item 7) |
| F39 | ✅ checked: PRAISE, Carnegie Mellon, Russinovich/Hanselman question | https://www.all-ai.de/news/news26/microsoft-praise-programmierer |
| F3, F5 | not checked: specific percentages (14–26%, 57–76%, 2.9k tokens) are worth a look | obsessive-inference-optimization |
| F40–F44 | not checked: added in s2, from three aihero skills pages | https://www.aihero.dev/skills-code-review, `/skills-handoff`, `/skills-to-tickets` |
| F37, F38 | not checked: paraphrased from German | https://www.all-ai.de/news/news26/google-harness-start |
| Gaps 1–12 | Read for judgement: do the cited findings actually support each gap? Not a mechanical check | `report.md` § "Gaps in fabrik" |

Selection: 5 findings drawn with `shuf -n 5` over all `### F` ids, using a fixed seed (`--random-source=<(yes 20261022)`), so it is reproducible. Content was checked on 2026-10-22, one day after the worker's fetch.

<!-- relay-audit verdict=pass -->
