# ADR 0063: Hosting Provider Registry — Extensible Adapters

**Status:** Proposed (2026-11-21)
**Affects:** ADR 0062 (hosting provider port/adapter), ADR 0056 (adapter registry at the
composition root), ADR 0057 (port/adapter pattern).

## Context

ADR 0062 introduced the hosting **port** (`Provider`) and two **adapters**
(`GitHubProvider`, `GitLabProvider`). Selection (`fabrik/hosting/select.py`) resolved the
provider with a hardcoded `if kind == "gitlab": … else: …` — it knows exactly two providers
and cannot take a third without a core code change.

Two gaps follow:

1. **No extension point.** A user with a Codeberg, Gitea, or a heavily customized GitLab
   instance cannot add an adapter without patching fabrik core. There is no registry to
   register a name → adapter mapping, and no discovery mechanism for third-party packages.
2. **High implementation bar.** The `Provider` ABC declares ~18 abstract methods, but a new
   host rarely supports all of them (labels, reviews, diffs, native dependencies, API commits
   are often N/A). A custom adapter had to stub every one, even the ones it does not
   implement.

The adapter-registry pattern is already established in the codebase (ADR 0056, deployment
profiles; ADR 0057, `RunLauncher`). This ADR applies the same pattern to hosting.

## Decision

1. **Provider registry (`fabrik/hosting/registry.py`).** A name → *factory* map. A **factory**
   has the signature `(repo, spec, role) -> Provider` and encapsulates all of that provider's
   construction (host resolution, credentials, …). Built-in adapters register themselves at
   import; `get_provider` collapses to *resolve a name, then call its factory*.

   ```python
   register_provider(name, factory, *, replace=False)  # validate + store
   get_factory(name)                                    # raises HostingConfigError if unknown
   registered_providers()                               # sorted names
   ```

   A factory (not a class) is used because each adapter constructs differently (GitHub takes
   `role`, GitLab takes `host`/`base_url`, a custom one might take a config object); the
   factory keeps each provider's construction quirks *with* the provider.

2. **Selection via the registry (`select.py`).** `get_provider` resolves the provider name
   (explicit `hosting:` spec field → repo-URL host inference → default GitHub) and delegates to
   `get_factory(name)(repo, spec, role)`. The `hosting:` field may now name **any registered
   provider**, not just `github`/`gitlab`. Host inference is unchanged (`github.com` → GitHub,
   any other host → GitLab), so existing specs are byte-for-byte unaffected.

3. **Third-party discovery via entry points.** A `fabrik-codeberg` package declares
   `[project.entry-points."fabrik.hosting_provider"] codeberg = "fabrik_codeberg:factory"`;
   `load_entry_points()` (called once at `fabrik.hosting` import) discovers it via
   `importlib.metadata`. A broken entry point is skipped, never fatal — provider selection must
   not crash because an optional plugin failed to load. In-process users can also call
   `register_provider(name, factory)` directly.

4. **`BaseProvider` lowers the implementation bar.** A new `BaseProvider(Provider)` provides
   `HostingNotSupported` defaults for every *optional* operation, so an adapter implements only
   the **core**: `parse_repo`, issues (`create_issue`/`get_issue`/`edit_issue`), and
   PRs/MRs (`create_pr`/`get_pr`/`merge_pr`). Everything else (labels, comments, listing,
   reviews, diffs, compare, native dependencies, API commits, `update_branch`) raises
   `HostingNotSupported` by default and is enabled by overriding the method.

5. **Shared host helper.** `repo_host(ref)` (host of a URL/SSH ref, `None` for a bare slug) and
   the `GITHUB_HOST` constant move to `base.py` so a factory can resolve its host without
   importing `select` (no circular import).

## Consequences

- (+) New hosts (Codeberg, Gitea, custom GitLab) are addable without a core change: subclass
  `BaseProvider`, implement the core, `register_provider` (or ship an entry point).
- (+) The implementation bar drops from ~18 stubs to the core a host actually supports.
- (+) Behaviour-preserving: the GitHub/GitLab path is unchanged (the registry just replaces the
  if/else with a lookup); existing specs and tests are unaffected.
- (+) The registry is host-agnostic and tiny — the same mechanism is reusable for other
  pluggable ports (see *Future work*).
- (−) The registry is global mutable state; tests that register providers must clean up
  (snapshot/restore `_REGISTRY`).
- (−) Entry-point discovery runs at import; a misbehaving plugin is skipped silently (logged
  nowhere), which can make a missing provider harder to diagnose.

## Future work

The registry is deliberately generic. When a concrete need for an **external issue tracker**
(Jira, Linear) alongside a code host arises, the same pattern extends to a separate
`IssueTracker` port + `register_issue_tracker(name, factory)`, with the hosting provider's
built-in issues as the default tracker (so nothing changes for existing users). That split is a
separate ADR and is **not** done here.
