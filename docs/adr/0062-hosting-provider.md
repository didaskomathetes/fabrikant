# ADR 0062: Hosting Provider — Port/Adapter for Code Hosting (GitHub, GitLab)

**Status:** Accepted (2026-11-21); proposed 2026-11-20, accepted on merge to main
**Affects:** ADR 0010 (dual identity/tokens), ADR 0011 (approval context), ADR 0027 (webhook
trigger), ADR 0031 (native issue dependencies), ADR 0057 (port/adapter pattern).

## Context

fabrik has historically talked to code hosting **directly over the GitHub REST API**: steps and
triggers call `gitops._gh_api` / `_gh_token` / `_gh_diff` / `_gh_dependencies` with raw
`api.github.com` URLs (`pr`, `issue`, `pr_merge`, `human_gate`, `notify`, `scheduler`,
`retry_chains`, `apicommit`, `relay`). That is correct for GitHub, but:

1. **No self-hosted GitLab.** A self-hosted GitLab (e.g. `gitlab.opencode.de`) cannot be used as a
   host — every call site would have to be rewritten by hand.
2. **Provider-specific JSON in the core.** Steps read GitHub fields (`head.sha`, `mergeable`,
   `labels[].name`, …) straight from the API response. That is fragile and not portable.
3. **No shared contract.** There is no single place defining the operations steps/triggers need
   from a host.

The port/adapter pattern (ADR 0057, `RunLauncher`; analogous to `AgentBackend`) is established in
the codebase and is adopted here for the hosting substrate.

## Decision

1. **Port `Provider` (`fabrik/hosting/base.py`).** The core defines the contract as the union of
   the operations steps and triggers need: issues (create/get/edit, labels, comments, list),
   PRs/MRs (create/get/list, merge, update-branch, files, diff, review, compare), native issue
   dependencies, API commits (`commit_via: api`), and webhook parsing. Structural (duck-typed)
   like `AgentBackend`/`RunLauncher` — adapters do not have to inherit.
2. **Normalized dataclasses as the contract.** `Issue`, `PullRequest`, `Comment`, `FileChange`,
   `CompareResult`, `WebhookEvent`, `RepoRef`. A step never sees provider JSON, only these types.
   Each type keeps the raw provider payload in `.raw` for the rare field with no common name
   (e.g. `mergeable_state`).
3. **Adapters `GitHubProvider` / `GitLabProvider`.**
   - `GitHubProvider` is a thin wrapper around the existing `gitops` helpers — the GitHub path
     stays byte-for-byte the previous one (no behavioural regression).
   - `GitLabProvider` implements the contract over the GitLab REST API v4
     (`https://<host>/api/v4`, `PRIVATE-TOKEN`).
4. **Selection + credentials (`fabrik/hosting/select.py`).** `get_provider(repo, spec, role)`
   picks the adapter: explicit `hosting:` spec field → repo-URL host (`github.com` → GitHub,
   otherwise GitLab) → `github:` field → default GitHub (backwards compatible). GitHub identity
   (dual identity, ADR 0010) via `role`; the GitLab token comes from `FABRIK_GITLAB_TOKEN` (env)
   or `~/.config/gitlab/token` (file, `FABRIK_GITLAB_TOKEN_FILE`).
5. **Spec binding per run.** The engine binds the active run spec via
   `hosting.spec_context(spec)` (a ContextVar) for the duration of a run — steps resolve the
   provider from it without the spec being threaded through every call (analogous to
   `gitops.project_github`).
6. **`commit_via: api` stays GitHub-only.** `apicommit.ApiPusher` rebuilds local commits as
   GitHub-signed commits via the Git Data API (Blobs → Tree → Commit → Ref); the signing is a
   GitHub property with no GitLab equivalent. `push_branch` now resolves the provider and raises
   `HostingNotSupported` for a non-GitHub host instead of silently assuming GitHub.
7. **Egress allowlist is deployment-scoped for GitLab.** The default-deny egress proxy
   (ADR 0051) ships `github.com` as a builtin. A self-hosted GitLab host is deployment-specific,
   so it is *not* a builtin: a deployment using GitLab adds its host to the egress allowlist
   (`FABRIK_EGRESS_ALLOWLIST` / `FABRIK_EGRESS_ALLOWLIST_FILE`; template
   `deploy/egress-allowlist.txt`), otherwise the proxy blocks agent access to it.

### Documented gaps (GitLab)

- `update_branch`: no GitLab equivalent → `HostingNotSupported` (merge the target into the source
  via git).
- `post_review`: `APPROVE` → the approve endpoint; `REQUEST_CHANGES` → a note (no distinct state).
- `merge_pr`: ignores `method` (squash/merge/rebase is a branch setting, not a merge parameter).
- Native issue dependencies: GitHub-only → `HostingNotSupported`.
- `edit_issue`: `state_reason`/`duplicate_issue_id` (GitHub close semantics) are ignored.
- `commit_via: api`: GitHub-only (see decision 6) → `HostingNotSupported`.

### Version & capability registration (GitLab)

`GitLabProvider` registers the host's version and capabilities once from `GET /version`
(`GitLabProvider.capabilities()` → `GitLabCapabilities`; the `version` property exposes the raw
string, e.g. `"19.5.8-ee"`). The version is parsed to a comparable tuple (`"19.5.8-ee"` →
`(19, 5, 8)`) and drives endpoint selection:

- **MR diffs.** The modern per-file endpoint (`GET .../merge_requests/:iid/versions` →
  `GET .../merge_request_diffs/:diff_id`) is used when the version supports it (GitLab ≥ 14).
  The diff is computed **asynchronously** after the MR is created, so the provider polls briefly
  (bounded, ~10 s) until it is collected. Restricted instances disable the modern endpoint
  (404/405) even on recent versions, so the provider falls back to the deprecated
  `GET .../merge_requests/:iid/changes` endpoint on 404/405.
- **Issue labels.** The `.../issues/:iid/labels` sub-endpoint is disabled on some instances; the
  provider uses the issue update's `add_labels` / `remove_labels` params (supported and
  idempotent) instead.

The `diff` field GitLab returns is the **hunk only** (no `diff --git` header), so a file's path is
read from `new_path` (exposed via `pr_files`), not from the diff text.

## Consequences

- (+) Self-hosted GitLab becomes usable as a host; `pr`, `issue`, `pr_merge` run over the same
  path on GitHub and GitLab.
- (+) Steps see only normalized types — switching providers touches no step code.
- (+) The GitHub path is unchanged (a wrapper over `gitops`); existing tests stay valid.
- (+) GitLab version/capability registration makes endpoint selection explicit and robust across
  versions and restricted instances (modern diffs endpoint with a deprecated fallback).
- (−) The port is the union of all operations — it grows as steps/triggers need more.
- (−) The GitLab gaps (`update_branch`, dependencies, `commit_via: api`) are real feature
  differences, not bugs; the affected features stay GitHub-only.
- (−) Migrating the remaining direct `gitops` calls (`human_gate`, `notify`, `scheduler`,
  `retry_chains`, `apicommit`, `relay`) onto the provider is incremental; until then both paths
  coexist.
